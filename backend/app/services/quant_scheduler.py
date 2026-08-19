"""Quant shadow/calibration daily scheduling + email (mirrors ``paper_service``).

* weekday ``quant_shadow_daily_time`` (default 17:30) — run ``python cli.py shadow``
  in the quant project root, read ``outputs/shadow_status.json``, email the daily
  report, publish ``quant_shadow_ran`` over the WebSocket bus;
* saturday ``quant_calibrate_time`` (default 18:00) — run ``python cli.py calibrate``,
  read ``outputs/s7_calibration.json``, email the report, publish ``quant_calibrated``.

APScheduler is **in-process**: the machine must be awake at the trigger time (the
"24/7" note in the plan). ``misfire_grace_time`` + ``coalesce=True`` let a missed
run catch up on the next wake, and the resumable FQA ledger backfills any missed
trading days.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import db, ws
from app.config import settings
from app.services import mailer, quant_manager
from app.services.llm_client import LLMClient

logger = logging.getLogger(__name__)

SHADOW_JOB_ID = "quant_shadow_daily"
CALIBRATE_JOB_ID = "quant_calibrate"

_scheduler: BackgroundScheduler | None = None
_started = False
_lock = threading.Lock()

_last_shadow_run: dict[str, Any] | None = None
_last_calibration_run: dict[str, Any] | None = None


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _parse_hhmm(value: Any, default: tuple[int, int]) -> tuple[int, int]:
    try:
        hh, mm = str(value).strip().split(":")
        h, m = int(hh), int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except Exception:  # noqa: BLE001
        pass
    return default


def _quant_root() -> Path:
    root = str(settings.get_quant_root() or "").strip()
    return Path(root) if root else Path(".")


def _read_report(rel: str) -> str:
    path = _quant_root() / rel
    if path.is_file():
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return ""
    return ""


def _shadow_summary_text(status: dict[str, Any]) -> str:
    eq = status.get("equity", {})
    lines = [
        "# FQA 影子模式日报",
        "",
        f"- 观察日期: {status.get('last_trading_date') or status.get('as_of')}",
        f"- 数据新鲜度: {status.get('data_freshness_days')} 天",
        f"- 最新净值: {eq.get('latest', 0):,.2f}",
        f"- 累计收益: {eq.get('total_return', 0):.2%}　Sharpe: {eq.get('sharpe', 0):.2f}　"
        f"最大回撤: {eq.get('max_drawdown', 0):.2%}",
        "",
    ]
    for rl in status.get("red_lines", []):
        lines.append(f"- [{rl.get('level')}] {rl.get('name')}: {rl.get('value')}　{rl.get('detail')}")
    return "\n".join(lines) + "\n"


def _calibration_summary_text(cal: dict[str, Any]) -> str:
    amp = cal.get("amplitude", {})
    sent = cal.get("sentiment", {})
    cost = cal.get("cost", {})
    lines = [
        "# FQA §7 回校报告",
        "",
        f"- 回校窗口: {cal.get('window', {}).get('start')} ~ {cal.get('window', {}).get('end')}",
        f"- 自动写回: {'是' if cal.get('auto_apply') else '否'}",
        f"- PEAD 幅度: {amp.get('current')} → {amp.get('recommended')}",
        f"- 舆情阈值: z={sent.get('recommended', {}).get('zscore_threshold')} "
        f"freeze={sent.get('recommended', {}).get('freeze_days')}",
        f"- 成本: 佣金 {cost.get('recommended', {}).get('commission_bps')}bps, "
        f"偏差 {cost.get('deviation_pct', 0):+.1f}%",
        "",
    ]
    changed = cal.get("applied", {}).get("changed", {})
    if changed:
        lines.append("写回项: " + ", ".join(changed.keys()))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# financial-expert commentary
# --------------------------------------------------------------------------- #
_FINANCIAL_SYSTEM = (
    "你是一位资深的 A 股量化交易与风险管理专家，擅长解读影子盘（模拟盘）的净值曲线、"
    "回撤、红线状态与持仓成本，并给出克制、可执行的改进建议。"
)


def _build_commentary_prompt(report: str) -> str:
    return (
        "以下是今日 FQA 影子模式量化交易的日报。请以资深 A 股量化交易与风控专家的身份，"
        "对这份报告进行专业点评，并给出可执行的改进建议。\n\n"
        "要求：\n"
        "1. 用中文输出，Markdown 格式，标题以「# 金融专家点评」开头；\n"
        "2. 点评今日组合表现、红线状态与潜在风险；\n"
        "3. 给出 3~5 条具体、可执行的改进建议；\n"
        "4. 语气专业、克制，不做任何收益承诺或投资建议的绝对保证。\n\n"
        "日报内容如下：\n\n" + report
    )


async def _commentary_chat(prompt: str) -> str:
    model = str(settings.get("quant_commentary_model") or "").strip()
    api_key = str(settings.get("quant_commentary_api_key") or "").strip()
    result = await LLMClient().chat(
        [{"role": "system", "content": _FINANCIAL_SYSTEM}, {"role": "user", "content": prompt}],
        model=model or None,
        api_key=api_key or None,
        temperature=0.4,
    )
    return str(result.get("content") or "").strip()


def _generate_shadow_commentary(report: str) -> str:
    """Ask the LLM (as a financial expert) to comment on the daily report.

    Returns empty string on any failure so the email still goes out without the
    commentary section.
    """
    try:
        content = asyncio.run(_commentary_chat(_build_commentary_prompt(report)))
        return content if content else ""
    except Exception:  # noqa: BLE001
        logger.exception("LLM shadow commentary failed; emailing without commentary")
        return ""


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #
def run_shadow_daily() -> dict[str, Any]:
    """Run the shadow mode and email the daily report."""
    global _last_shadow_run
    result: dict[str, Any] = {"ok": False}
    try:
        proc = quant_manager.run_project_command("python cli.py shadow", timeout=3600)
        status = quant_manager.read_shadow_status()
        report = _read_report("outputs/shadow_report.md")

        email: dict[str, Any] = {"ok": False, "error": "auto-email disabled"}
        if settings.get_bool("quant_shadow_auto_email", True) and status is not None:
            if not report:
                report = _shadow_summary_text(status)
            if settings.get_bool("quant_commentary_enabled", True):
                commentary = _generate_shadow_commentary(report)
                if commentary:
                    report = report.rstrip() + "\n\n---\n\n" + commentary
            subject = f"FQA 影子模式日报 — {status.get('last_trading_date') or status.get('as_of')}"
            email = mailer.send_mail(subject, report)

        result = {
            "ok": proc.get("returncode") == 0,
            "command": proc.get("command"),
            "returncode": proc.get("returncode"),
            "last_trading_date": status.get("last_trading_date") if status else None,
            "as_of": status.get("as_of") if status else None,
            "emailed": bool(email.get("ok")),
            "email": email,
            "stderr_tail": (proc.get("stderr") or "")[-500:],
        }
        db.log_operation("quant_shadow_run", {}, {k: v for k, v in result.items() if k != "stderr_tail"})
        ws.publish("quant_shadow_ran", result)
    except Exception as exc:  # noqa: BLE001
        logger.exception("shadow daily run failed")
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        ws.publish("quant_shadow_ran", result)
    _last_shadow_run = result
    return result


def run_calibration() -> dict[str, Any]:
    """Run the §7 calibration and email the report."""
    global _last_calibration_run
    result: dict[str, Any] = {"ok": False}
    try:
        proc = quant_manager.run_project_command("python cli.py calibrate", timeout=3600)
        cal = quant_manager.read_s7_calibration()
        report = _read_report("outputs/s7_calibration.md")

        email: dict[str, Any] = {"ok": False, "error": "no result"}
        if cal is not None:
            if not report:
                report = _calibration_summary_text(cal)
            email = mailer.send_mail(f"FQA §7 回校报告 — {cal.get('as_of')}", report)

        result = {
            "ok": proc.get("returncode") == 0,
            "returncode": proc.get("returncode"),
            "as_of": cal.get("as_of") if cal else None,
            "applied": cal.get("applied", {}).get("changed", {}) if cal else {},
            "emailed": bool(email.get("ok")),
            "email": email,
            "stderr_tail": (proc.get("stderr") or "")[-500:],
        }
        db.log_operation("quant_calibration_run", {},
                         {k: v for k, v in result.items() if k not in ("stderr_tail", "applied")})
        ws.publish("quant_calibrated", result)
    except Exception as exc:  # noqa: BLE001
        logger.exception("calibration run failed")
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        ws.publish("quant_calibrated", result)
    _last_calibration_run = result
    return result


# --------------------------------------------------------------------------- #
# scheduler + status
# --------------------------------------------------------------------------- #
def start_scheduler() -> None:
    """Idempotently start the shadow + calibration schedulers."""
    global _scheduler, _started
    with _lock:
        if _started:
            return
        try:
            scheduler = BackgroundScheduler()
            dh, dm = _parse_hhmm(settings.get("quant_shadow_daily_time"), (17, 30))
            ch, cm = _parse_hhmm(settings.get("quant_calibrate_time"), (18, 0))
            scheduler.add_job(
                run_shadow_daily,
                CronTrigger(day_of_week="mon-fri", hour=dh, minute=dm),
                id=SHADOW_JOB_ID, replace_existing=True,
                misfire_grace_time=7200, coalesce=True,
            )
            scheduler.add_job(
                run_calibration,
                CronTrigger(day_of_week="sat", hour=ch, minute=cm),
                id=CALIBRATE_JOB_ID, replace_existing=True,
                misfire_grace_time=86400, coalesce=True,
            )
            scheduler.start()
            _scheduler = scheduler
            _started = True
            logger.info(
                "quant scheduler started (shadow mon-fri %02d:%02d, calibrate sat %02d:%02d)",
                dh, dm, ch, cm,
            )
        except Exception:  # noqa: BLE001
            logger.exception("failed to start quant scheduler")


def get_status() -> dict[str, Any]:
    dh, dm = _parse_hhmm(settings.get("quant_shadow_daily_time"), (17, 30))
    ch, cm = _parse_hhmm(settings.get("quant_calibrate_time"), (18, 0))
    return {
        "shadow_daily_time": f"{dh:02d}:{dm:02d}",
        "calibrate_time": f"{ch:02d}:{cm:02d}",
        "shadow_auto_email": settings.get_bool("quant_shadow_auto_email", True),
        "scheduler_running": _started,
        "last_shadow_run": _last_shadow_run,
        "last_calibration_run": _last_calibration_run,
    }

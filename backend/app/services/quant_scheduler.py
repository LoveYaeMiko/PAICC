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
from datetime import datetime
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
WEEKLY_JOB_ID = "quant_weekly_cycle"

_scheduler: BackgroundScheduler | None = None
_started = False
_lock = threading.Lock()

_last_shadow_run: dict[str, Any] | None = None
_last_calibration_run: dict[str, Any] | None = None
_last_autopilot_run: dict[str, Any] | None = None
_last_weekly_run: dict[str, Any] | None = None


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
        lines.append(f"- [{rl.get('level')}] {rl.get('label') or rl.get('name')}: {rl.get('value')}　{rl.get('detail')}")
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


def _failure_email_text(title: str, proc: dict[str, Any]) -> str:
    """Body for a daily-run *failure* alert — never forward a stale report.

    When ``python cli.py autopilot``/``shadow`` fails (e.g. the PIT database is
    unreachable), the on-disk report is yesterday's — emailing it as "today's"
    report would silently duplicate stale numbers. Alert the operator instead.
    """
    lines = [
        f"# {title}",
        "",
        f"- 时间: {datetime.now().isoformat(timespec='seconds')}",
        f"- 命令: `{proc.get('command')}`",
        f"- 返回码: {proc.get('returncode')}",
        "",
        "## 错误输出（尾部）",
        "```",
        (proc.get('stderr') or '')[-800:],
        "```",
    ]
    return "\n".join(lines) + "\n"


def _ensure_pit_db_or_fail() -> dict[str, Any] | None:
    """Ensure the PIT database is up before a daily run.

    Returns ``None`` when the DB is ready. When Docker can't be brought up, returns
    a synthetic failure ``proc`` (``returncode=None``) so the caller emits the
    operator failure alert and skips a CLI run that would otherwise die on an
    unreachable PIT store — the *actual* root cause of the stale-report loop.
    """
    db_up = quant_manager.ensure_pit_db_up()
    if db_up.get("ok"):
        return None
    return {
        "command": "ensure PIT DB (docker compose up -d)",
        "returncode": None,
        "stdout": "",
        "stderr": f"PIT 数据库不可用 [{db_up.get('stage')}]: {db_up.get('detail')}",
    }


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
# autopilot (end-to-end adaptive closed loop)
# --------------------------------------------------------------------------- #
def _autopilot_summary_text(state: dict[str, Any] | None, status: dict[str, Any] | None) -> str:
    """Fallback text body when FQA has not yet written ``autopilot_report.md``."""
    state = state or {}
    status = status or {}
    eq = status.get("equity", {})
    scale = state.get("gross_scale")
    scale_txt = f"{scale:g}" if isinstance(scale, (int, float)) else "1"
    lines = [
        "# FQA 自动闭环日报",
        "",
        f"- 运行时间: {state.get('last_evaluated') or state.get('since_date')}",
        f"- 观察日期: {status.get('last_trading_date') or status.get('as_of')}",
        f"- 当前档位: {state.get('mode', 'normal')}（总敞口 ×{scale_txt}）",
        f"- 最新净值: {eq.get('latest', 0):,.2f}　累计收益: {eq.get('total_return', 0):.2%}",
        f"- 因子衰减: {'是' if state.get('factor_decayed') else '否'}",
    ]
    reason = state.get("reason")
    if reason:
        lines.append(f"- 档位原因: {reason}")
    return "\n".join(lines) + "\n"


def run_autopilot_daily() -> dict[str, Any]:
    """Run FQA's dual-track end-to-end autopilot loop and email the report.

    ``python cli.py autopilot`` advances each account's shadow (honouring its last
    kill-switch decision), re-evaluates the per-account risk gate and persists the
    operating mode (``autopilot_state_<name>.json``). ML accounts iterate their
    model via the Sunday weekly job; factor-pool periodic tasks only run for pool
    accounts. It is the single daily entry point that closes the loop.
    """
    global _last_autopilot_run
    result: dict[str, Any] = {"ok": False}
    ws.publish("quant_autopilot_started", {"ts": datetime.now().isoformat(timespec="seconds")})
    try:
        proc = _ensure_pit_db_or_fail() or quant_manager.run_project_command("python cli.py autopilot", timeout=7200)
        ok = proc.get("returncode") == 0
        states = quant_manager.read_autopilot_states()
        accounts = quant_manager.read_shadow_accounts()
        report_parts: list[str] = []
        for name, state in states.items():
            st = (accounts.get(name) or {}).get("status") or {}
            rep = _read_report(f"outputs/autopilot_report_{name}.md")
            if not rep:
                rep = _autopilot_summary_text(state, st)
            report_parts.append(f"## 账户 {name}\n\n{rep}")
        report = "\n\n".join(report_parts) if report_parts else ""

        email: dict[str, Any] = {"ok": False, "error": "auto-email disabled"}
        if settings.get_bool("quant_shadow_auto_email", True):
            if ok:
                if not report:
                    report = _autopilot_summary_text(None, None)
                if settings.get_bool("quant_commentary_enabled", True):
                    commentary = _generate_shadow_commentary(report)
                    if commentary:
                        report = report.rstrip() + "\n\n---\n\n" + commentary
                first = next(iter(states.values()), {}) or {}
                date = first.get("last_evaluated")
                email = mailer.send_mail(f"FQA 双资金轨自动闭环日报 — {date}", report)
            else:
                # Run failed (PIT DB down, etc.) — the on-disk report is stale, so
                # alert the operator instead of forwarding yesterday's numbers.
                email = mailer.send_mail(
                    "FQA 自动闭环运行失败", _failure_email_text("FQA 自动闭环运行失败", proc)
                )

        result = {
            "ok": ok,
            "returncode": proc.get("returncode"),
            "accounts": sorted(states.keys()),
            "modes": {n: (s or {}).get("mode") for n, s in states.items()},
            "emailed": bool(email.get("ok")),
            "email": email,
            "stderr_tail": (proc.get("stderr") or "")[-500:],
        }
        db.log_operation("quant_autopilot_run", {}, {k: v for k, v in result.items() if k != "stderr_tail"})
        ws.publish("quant_autopilot_ran", result)
    except Exception as exc:  # noqa: BLE001
        logger.exception("autopilot daily run failed")
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        ws.publish("quant_autopilot_ran", result)
    _last_autopilot_run = result
    return result


def run_weekly_cycle() -> dict[str, Any]:
    """Weekly auto closed-loop: fold the week's data into history and re-optimise.

    Runs ``python cli.py weekly`` in the FQA root — retrains the LightGBM with
    data through the latest bar and promotes the new artifact only if its
    trailing Sharpe improves on the incumbent. Emails the weekly result.
    """
    global _last_weekly_run
    result: dict[str, Any] = {"ok": False}
    ws.publish("quant_weekly_started", {"ts": datetime.now().isoformat(timespec="seconds")})
    try:
        proc = _ensure_pit_db_or_fail() or quant_manager.run_project_command("python cli.py weekly", timeout=7200)
        ok = proc.get("returncode") == 0
        weekly_json = _read_report("outputs/weekly.json")
        import json as _json

        weekly: dict[str, Any] = {}
        try:
            weekly = _json.loads(weekly_json) if weekly_json else {}
        except ValueError:
            weekly = {}

        email: dict[str, Any] = {"ok": False, "error": "auto-email disabled"}
        if settings.get_bool("quant_shadow_auto_email", True):
            if ok:
                subject = (
                    f"FQA 周度闭环 — {weekly.get('date')} "
                    f"{'promoted' if weekly.get('promoted') else 'kept'} "
                    f"(sharpe {weekly.get('new_sharpe', 0):.3f})"
                )
                body = (
                    f"# FQA 周度自动闭环\n\n"
                    f"- 数据截至: {weekly.get('data_through')}\n"
                    f"- 基线 Sharpe: {weekly.get('baseline_sharpe', 0):.3f}\n"
                    f"- 新 Sharpe: {weekly.get('new_sharpe', 0):.3f}\n"
                    f"- promote: {'是' if weekly.get('promoted') else '否（保留原工件）'}\n"
                )
                email = mailer.send_mail(subject, body)
            else:
                email = mailer.send_mail(
                    "FQA 周度闭环运行失败", _failure_email_text("FQA 周度闭环运行失败", proc)
                )

        result = {
            "ok": ok,
            "returncode": proc.get("returncode"),
            "promoted": bool(weekly.get("promoted", False)),
            "baseline_sharpe": weekly.get("baseline_sharpe"),
            "new_sharpe": weekly.get("new_sharpe"),
            "data_through": weekly.get("data_through"),
            "emailed": bool(email.get("ok")),
            "stderr_tail": (proc.get("stderr") or "")[-500:],
        }
        db.log_operation("quant_weekly_run", {}, {k: v for k, v in result.items() if k != "stderr_tail"})
        ws.publish("quant_weekly_ran", result)
    except Exception as exc:  # noqa: BLE001
        logger.exception("weekly cycle failed")
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        ws.publish("quant_weekly_ran", result)
    _last_weekly_run = result
    return result


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #
def run_shadow_daily() -> dict[str, Any]:
    """Run the dual-track shadow mode and email the combined daily report."""
    global _last_shadow_run
    result: dict[str, Any] = {"ok": False}
    ws.publish("quant_shadow_started", {"ts": datetime.now().isoformat(timespec="seconds")})
    try:
        proc = _ensure_pit_db_or_fail() or quant_manager.run_project_command("python cli.py shadow", timeout=7200)
        ok = proc.get("returncode") == 0
        accounts = quant_manager.read_shadow_accounts()
        report_parts: list[str] = []
        date: str | None = None
        for name, entry in accounts.items():
            st = entry.get("status") or {}
            if not date:
                date = st.get("last_trading_date") or st.get("as_of")
            rep = entry.get("report") or _shadow_summary_text(st)
            report_parts.append(f"## 账户 {name}\n\n{rep}")
        report = "\n\n".join(report_parts) if report_parts else ""

        email: dict[str, Any] = {"ok": False, "error": "auto-email disabled"}
        if settings.get_bool("quant_shadow_auto_email", True):
            if ok and report:
                if settings.get_bool("quant_commentary_enabled", True):
                    commentary = _generate_shadow_commentary(report)
                    if commentary:
                        report = report.rstrip() + "\n\n---\n\n" + commentary
                email = mailer.send_mail(f"FQA 双资金轨影子日报 — {date}", report)
            elif not ok:
                email = mailer.send_mail(
                    "FQA 影子模式运行失败", _failure_email_text("FQA 影子模式运行失败", proc)
                )

        result = {
            "ok": ok,
            "command": proc.get("command"),
            "returncode": proc.get("returncode"),
            "accounts": sorted(accounts.keys()),
            "last_trading_date": date,
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
    ws.publish("quant_calibration_started", {"ts": datetime.now().isoformat(timespec="seconds")})
    try:
        proc = _ensure_pit_db_or_fail() or quant_manager.run_project_command("python cli.py calibrate", timeout=3600)
        ok = proc.get("returncode") == 0
        cal = quant_manager.read_s7_calibration()
        report = _read_report("outputs/s7_calibration.md")

        email: dict[str, Any] = {"ok": False, "error": "no result"}
        if ok and cal is not None:
            if not report:
                report = _calibration_summary_text(cal)
            email = mailer.send_mail(f"FQA §7 回校报告 — {cal.get('as_of')}", report)
        elif not ok:
            email = mailer.send_mail(
                "FQA §7 回校运行失败", _failure_email_text("FQA §7 回校运行失败", proc)
            )

        result = {
            "ok": ok,
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
def _fire_missed_today_jobs() -> None:
    """Catch up on scheduled jobs missed while the backend was down.

    The in-process APScheduler only fires while the backend process lives, and
    the Electron main spawns/kills the backend with the app window — so a closed
    PAICC means the 17:30 daily loop never ran. On startup, check the operation
    log for today's run (or a catch-up marker) and fire each missed job once.
    """
    import time as _time

    now = datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start = _time.mktime(midnight.timetuple())

    def ran_any_today(actions: tuple[str, ...]) -> bool:
        marks = ", ".join("?" for _ in actions)
        rows = db.query(
            f"SELECT id FROM operation_logs WHERE action IN ({marks}) AND timestamp >= ? LIMIT 1",
            (*actions, day_start),
        )
        return bool(rows)

    autopilot = settings.get_bool("quant_autopilot_enabled", True)
    daily_job = run_autopilot_daily if autopilot else run_shadow_daily
    dh, dm = _parse_hhmm(settings.get("quant_shadow_daily_time"), (17, 30))
    ch, cm = _parse_hhmm(settings.get("quant_calibrate_time"), (18, 0))
    wh, wm = _parse_hhmm(settings.get("quant_weekly_time"), (18, 0))

    def fire_once(marker_action: str, check_actions: tuple[str, ...], job) -> None:
        if ran_any_today(check_actions):
            return
        db.log_operation(marker_action, {"date": now.strftime("%Y-%m-%d")}, {"fired": True})
        threading.Thread(target=job, daemon=True, name=f"quant-catchup-{marker_action}").start()
        logger.info("catch-up: missed %s fired", marker_action)

    if now.weekday() < 5 and (now.hour, now.minute) >= (dh, dm):
        fire_once(
            "quant_catchup_daily",
            ("quant_autopilot_run", "quant_shadow_run", "quant_catchup_daily"),
            daily_job,
        )
    if now.weekday() == 5 and (now.hour, now.minute) >= (ch, cm):
        fire_once(
            "quant_catchup_calibrate",
            ("quant_calibration_run", "quant_catchup_calibrate"),
            run_calibration,
        )
    if now.weekday() == 6 and (now.hour, now.minute) >= (wh, wm):
        fire_once(
            "quant_catchup_weekly",
            ("quant_weekly_run", "quant_catchup_weekly"),
            run_weekly_cycle,
        )


def start_scheduler() -> None:
    """Idempotently start the shadow + calibration schedulers + startup catch-up."""
    global _scheduler, _started
    with _lock:
        if _started:
            return
        try:
            scheduler = BackgroundScheduler()
            dh, dm = _parse_hhmm(settings.get("quant_shadow_daily_time"), (17, 30))
            ch, cm = _parse_hhmm(settings.get("quant_calibrate_time"), (18, 0))
            autopilot = settings.get_bool("quant_autopilot_enabled", True)
            daily_job = run_autopilot_daily if autopilot else run_shadow_daily
            scheduler.add_job(
                daily_job,
                CronTrigger(day_of_week="mon-fri", hour=dh, minute=dm),
                id=SHADOW_JOB_ID, replace_existing=True,
                # 24h grace: if the machine was asleep at 17:30 and wakes later
                # (even the next morning), fire the missed daily loop once —
                # the resumable FQA ledger backfills the missed trading days.
                misfire_grace_time=86400, coalesce=True,
            )
            scheduler.add_job(
                run_calibration,
                CronTrigger(day_of_week="sat", hour=ch, minute=cm),
                id=CALIBRATE_JOB_ID, replace_existing=True,
                misfire_grace_time=86400, coalesce=True,
            )
            wh, wm = _parse_hhmm(settings.get("quant_weekly_time"), (18, 0))
            scheduler.add_job(
                run_weekly_cycle,
                CronTrigger(day_of_week="sun", hour=wh, minute=wm),
                id=WEEKLY_JOB_ID, replace_existing=True,
                misfire_grace_time=86400, coalesce=True,
            )
            scheduler.start()
            _scheduler = scheduler
            _started = True
            logger.info(
                "quant scheduler started (%s mon-fri %02d:%02d, calibrate sat %02d:%02d, weekly sun %02d:%02d)",
                "autopilot" if autopilot else "shadow", dh, dm, ch, cm, wh, wm,
            )
        except Exception:  # noqa: BLE001
            logger.exception("failed to start quant scheduler")
            return
    # Startup catch-up — a backend that was down at the scheduled time (the app
    # window closed) fires the missed job once instead of silently skipping the day.
    try:
        _fire_missed_today_jobs()
    except Exception:  # noqa: BLE001
        logger.exception("scheduler catch-up check failed")


def get_status() -> dict[str, Any]:
    dh, dm = _parse_hhmm(settings.get("quant_shadow_daily_time"), (17, 30))
    ch, cm = _parse_hhmm(settings.get("quant_calibrate_time"), (18, 0))
    wh, wm = _parse_hhmm(settings.get("quant_weekly_time"), (18, 0))
    return {
        "shadow_daily_time": f"{dh:02d}:{dm:02d}",
        "calibrate_time": f"{ch:02d}:{cm:02d}",
        "weekly_time": f"{wh:02d}:{wm:02d}",
        "shadow_auto_email": settings.get_bool("quant_shadow_auto_email", True),
        "autopilot_enabled": settings.get_bool("quant_autopilot_enabled", True),
        "scheduler_running": _started,
        "last_shadow_run": _last_shadow_run,
        "last_calibration_run": _last_calibration_run,
        "last_autopilot_run": _last_autopilot_run,
        "last_weekly_run": _last_weekly_run,
    }

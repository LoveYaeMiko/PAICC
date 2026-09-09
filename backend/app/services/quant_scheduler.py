"""Quant **single-track (D)** daily scheduling + email.

FQA converged on one track: the A/B/C ML cross-section tracks retired on
2026-09-08 and ``shadow.accounts`` now holds only ``D_5W``
(``alpha_source: pullback``). D is still a **shadow/paper track — no real money
is wired in**; every report/email below describes simulated results.

The in-process APScheduler drives:

* weekday 09:25 — launch the real-time intraday trader (``cli.py live``);
* weekday 14:40 — AlphaFeed depth snapshot;
* weekday 14:50 — closing-auction order list (``cli.py preclose``);
* weekday 15:02 — intraday feature rollup refresh;
* weekday ``quant_shadow_daily_time`` (default 15:10) — ``python cli.py
  autopilot`` (or ``shadow`` when autopilot is disabled), read the outputs,
  email the daily report, publish ``quant_shadow_ran`` over the WebSocket bus;
* weekday 17:45 — D-track model challenger (parallel shadow book);
* saturday ``quant_calibrate_time`` (default 18:00) — cost-model consistency
  audit (replaces the legacy §7 recalibration);
* sunday ``quant_weekly_time`` (default 18:00) — D-track monthly model cycle
  (challenger promotion gate + rolling refit).

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
from apscheduler.triggers.interval import IntervalTrigger

from app import db, ws
from app.config import settings
from app.services import mailer, quant_manager, trading_calendar
from app.services.llm_client import LLMClient

logger = logging.getLogger(__name__)

SHADOW_JOB_ID = "quant_shadow_daily"
CALIBRATE_JOB_ID = "quant_calibrate"
WEEKLY_JOB_ID = "quant_weekly_cycle"
DEPTH_JOB_ID = "quant_depth_snapshot"
LIVE_JOB_ID = "quant_live_start"
INTRA_JOB_ID = "quant_intraday_refresh"
CHALLENGER_JOB_ID = "quant_d_challenger"
PRECLOSE_JOB_ID = "quant_preclose"
WATCHDOG_JOB_ID = "quant_live_watchdog"
FORWARD_CANDIDATE_JOB_ID = "quant_forward_candidate"
FORWARD_HEALTH_JOB_ID = "quant_forward_health"


def is_trading_day(now: datetime | None = None) -> bool:
    """Weekday AND not an exchange holiday (see trading_calendar)."""
    return trading_calendar.is_trading_day(
        now or datetime.now(), settings.get("quant_holidays", "")
    )


def run_preclose_daily() -> dict[str, Any]:
    """Weekday 14:50 — decide the D-track closing-auction order list.

    ``cli.py preclose`` computes the close-rebalance orders from 14:50-known
    data (provisional minute bars + T-1 ML ranks) and persists them; the
    daily close run then fills exactly that list at the 15:00 auction close.
    Started 10 minutes before the auction because the full-universe minute
    fetch + market build takes ~5-8 minutes. A late/retroactive run is
    refused: orders decided AFTER the auction would trade on information a
    real 14:57 order could not have had.
    """
    import time as _time

    now = datetime.now()
    if not is_trading_day(now):
        return {"ok": True, "skipped": f"not a trading day ({now:%Y-%m-%d}) — no orders"}
    # 14:56 hard stop (audit finding): a run started after that cannot fetch and
    # submit before the 15:00 auction, and the FQA time guard only warns.
    if not ((14, 40) <= (now.hour, now.minute) <= (14, 56)):
        return {"ok": True, "skipped": f"outside 14:40-14:56 ({now:%H:%M}) — no retroactive orders"}
    last_err = ""
    while True:
        try:
            proc = _ensure_pit_db_or_fail() or quant_manager.run_project_command(
                "python cli.py preclose", timeout=900
            )
            ok = proc.get("returncode") == 0
            db.log_operation(
                "quant_preclose", {},
                {"ok": ok, "tail": (proc.get("stdout") or "")[-300:]},
            )
            return {"ok": ok}
        except Exception as exc:  # noqa: BLE001
            logger.exception("preclose order layer failed")
            last_err = f"{type(exc).__name__}: {exc}"
        # retry while a fresh attempt can still FETCH before the 15:00
        # auction (the CLI takes ~5-8 min); stop by 14:56 otherwise.
        now = datetime.now()
        if not (now.hour == 14 and now.minute <= 56):
            db.log_operation("quant_preclose", {}, {"ok": False, "error": last_err})
            return {"ok": False, "error": last_err}
        _time.sleep(30)


def start_live_trader() -> dict[str, Any]:
    """Weekday 09:25 — launch the FQA real-time intraday trader (detached).

    ``python cli.py live`` polls the latest minute print and executes D-track
    stop breaches at the actual moment; its decision window ends at 15:00 (the
    closing auction belongs to the 14:50 preclose order layer) and a pid lock
    makes double-starts no-ops. The PIT store is ensured FIRST — ``cli.py live``
    hard-exits on an unreachable PIT DB, so this job launches Docker Desktop
    itself and RETRIES every 45s (Docker cold starts after a long idle can take
    minutes; on 2026-09-08 the single 09:26 attempt gave up mid-daemon-start and
    the whole morning session was lost). Every retry is forward-only, so the
    no-past-timestamp discipline holds regardless of when it succeeds.
    """
    import time as _time

    last_err = ""
    attempts = 0
    if not is_trading_day():
        return {"ok": True, "skipped": "not a trading day — live trader not started"}
    while True:
        attempts += 1
        try:
            pit_fail = _ensure_pit_db_or_fail()
            if pit_fail is not None:
                last_err = str(pit_fail.get("stderr", ""))[-200:]
            else:
                proc = quant_manager.run_command(command="python cli.py live")
                db.log_operation("quant_live_start", {}, {"pid": proc.get("pid")})
                return {"ok": True, "pid": proc.get("pid")}
        except Exception as exc:  # noqa: BLE001
            logger.exception("live trader launch failed")
            last_err = f"{type(exc).__name__}: {exc}"
        now = datetime.now()
        morning_retry_ok = now.hour == 9 and now.minute < 56
        midday_retry_ok = now.hour >= 10 and attempts < 3
        if not (morning_retry_ok or midday_retry_ok):
            db.log_operation("quant_live_start", {}, {"ok": False, "error": last_err})
            return {"ok": False, "error": last_err}
        _time.sleep(45)


def refresh_intraday_daily_job() -> dict[str, Any]:
    """Weekday 15:02 — refresh the intraday feature rollup right after the close.

    The D-track tail-volume entry gate treats a missing current-day row as
    FAIL (no new entries), so this job fetches the latest minute bars and
    rebuilds ``data/intraday/daily_features.parquet`` BEFORE the 15:10 loop.
    The 15:10 loop additionally self-heals via ``ensure_intraday_current``,
    so a missed 15:02 never starves the gate.
    """
    try:
        proc = quant_manager.run_project_command(
            "python scripts/refresh_intraday_daily.py", timeout=1800
        )
        ok = proc.get("returncode") == 0
        db.log_operation(
            "quant_intraday_refresh", {},
            {"ok": ok, "tail": (proc.get("stdout") or "")[-200:]},
        )
        return {"ok": ok}
    except Exception as exc:  # noqa: BLE001
        logger.exception("intraday refresh failed")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

_scheduler: BackgroundScheduler | None = None
_started = False
_lock = threading.Lock()

_last_shadow_run: dict[str, Any] | None = None
_last_calibration_run: dict[str, Any] | None = None
_last_autopilot_run: dict[str, Any] | None = None
_last_weekly_run: dict[str, Any] | None = None
#: Per-job last result for the jobs that previously only wrote the operation log
#: (live / depth / preclose / intraday / challenger / watchdog).
_last_job_runs: dict[str, dict[str, Any]] = {}


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


def _stamp(result: dict[str, Any]) -> dict[str, Any]:
    """Tag a job result with its own wall-clock time.

    The panel's 「任务调度」 card reports ``last_run`` from the in-memory
    ``_last_*_run`` records, so each job stamps the moment it finished (an
    existing key is never overwritten).
    """
    result.setdefault("ts", datetime.now().isoformat(timespec="seconds"))
    return result


def _recording(job_id: str, fn):
    """Wrap a job so its result is remembered for the panel's job table."""

    def _wrapped():
        result = fn()
        if isinstance(result, dict):
            _last_job_runs[job_id] = dict(result)
        return result

    _wrapped.__name__ = getattr(fn, "__name__", job_id)
    return _wrapped


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
        "# FQA D 轨影子日报（模拟盘）",
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
        "# FQA D 轨自动闭环日报（模拟盘）",
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
    """Run FQA's single-track (D) end-to-end autopilot loop and email the report.

    ``python cli.py autopilot`` advances the D shadow book (honouring its last
    kill-switch decision), re-evaluates the risk gate and persists the operating
    mode (``autopilot_state_<name>.json``). The A/B/C ML tracks retired
    2026-09-08, so ``shadow.accounts`` holds only ``D_5W``; the loop still
    iterates whatever accounts the config lists, so the body degrades cleanly to
    a single 「## 账户 D_5W」 section. It is the single daily entry point that
    closes the loop. D remains a **simulated** book — no real money.
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
                email = mailer.send_mail(f"FQA D 轨自动闭环日报 — {date}", report)
            else:
                # Run failed (PIT DB down, etc.) — the on-disk report is stale, so
                # alert the operator instead of forwarding yesterday's numbers.
                email = mailer.send_mail(
                    "FQA D 轨自动闭环运行失败",
                    _failure_email_text("FQA D 轨自动闭环运行失败", proc),
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
    _last_autopilot_run = _stamp(result)
    return result


def run_weekly_cycle() -> dict[str, Any]:
    """Monthly (first Sunday) D-track model self-optimization cycle.

    Replaces the old weekly retrain: ``cli.py dcycle decide`` runs the forward
    promotion gate (challenger vs the REAL D ledger over the trailing OOS
    window), then ``cli.py dcycle refit`` folds the latest data into a rolling
    refit for the NEXT month's challenger. Non-first Sundays are a no-op so the
    weekly slot stays calendar-anchored.
    """
    global _last_weekly_run
    result: dict[str, Any] = {"ok": False}
    ws.publish("quant_weekly_started", {"ts": datetime.now().isoformat(timespec="seconds")})
    try:
        if not settings.get_bool("quant_d_cycle_enabled", False):
            result = {"ok": True, "skipped": "D 轨模型自优化循环未启用（quant_d_cycle_enabled=false）"}
            ws.publish("quant_weekly_ran", result)
            _last_weekly_run = _stamp(result)
            return result
        now = datetime.now()
        if now.day > 7:
            result = {"ok": True, "skipped": "非本月第一个周日 — 跳过月度模型循环"}
            ws.publish("quant_weekly_ran", result)
            _last_weekly_run = _stamp(result)
            return result

        proc = _ensure_pit_db_or_fail() or quant_manager.run_project_command(
            "python cli.py dcycle decide", timeout=3600
        )
        ok_decide = proc.get("returncode") == 0
        proc2 = _ensure_pit_db_or_fail() or quant_manager.run_project_command(
            "python cli.py dcycle refit", timeout=7200
        )
        ok_refit = proc2.get("returncode") == 0
        ok = ok_decide and ok_refit

        import json as _json

        cycle: dict[str, Any] = {}
        cycle_json = _read_report("outputs/d_model_cycle.json")
        if cycle_json:
            try:
                loaded = _json.loads(cycle_json)
                hist = loaded.get("history", []) if isinstance(loaded, dict) else []
                if hist:
                    cycle = hist[-1]
            except ValueError:
                cycle = {}

        email: dict[str, Any] = {"ok": False, "error": "auto-email disabled"}
        if settings.get_bool("quant_shadow_auto_email", True):
            if ok:
                subject = (
                    f"FQA D 轨模型月度循环 — {cycle.get('date', '?')} "
                    f"{'晋升' if cycle.get('promote') else '留任现役'}"
                    f"（Δ {cycle.get('delta_pp', 0):+.2f}pp）"
                )
                body = (
                    f"# FQA D 轨模型月度自优化\n\n"
                    f"- 评估窗: {cycle.get('window')}\n"
                    f"- 挑战者收益: {cycle.get('challenger_return', 0):.2%} vs "
                    f"现役 {cycle.get('incumbent_return', 0):.2%}\n"
                    f"- Δ {cycle.get('delta_pp', 0):+.2f}pp（边际 {cycle.get('margin_pp', 0):+.2f}pp）\n"
                    f"- 违规: {cycle.get('violations', {})}\n"
                    f"- 决策: {'晋升 ' + str(cycle.get('promoted_artifact', '')) if cycle.get('promote') else '留任现役（挑战者弃用）'}\n"
                )
                email = mailer.send_mail(subject, body)
            else:
                email = mailer.send_mail(
                    "FQA D 轨模型月度循环失败",
                    _failure_email_text("FQA D 轨模型月度循环失败",
                                        proc2 if not ok_refit else proc),
                )

        result = {
            "ok": ok,
            "ok_decide": ok_decide,
            "ok_refit": ok_refit,
            "decision": {k: v for k, v in cycle.items() if k != "violations"},
            "emailed": bool(email.get("ok")),
        }
        db.log_operation("quant_weekly_run", {}, {k: v for k, v in result.items()})
        ws.publish("quant_weekly_ran", result)
    except Exception as exc:  # noqa: BLE001
        logger.exception("weekly cycle failed")
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        ws.publish("quant_weekly_ran", result)
    _last_weekly_run = _stamp(result)
    return result


def run_forward_candidate_daily() -> dict[str, Any]:
    """Weekday 15:20 — advance the forward-period candidate shadow.

    Isolated ledger (``outputs/forward/candidate_atr_1p0_25_40/``), same data /
    code / execution regime as production, ONE difference (stop width), record
    only — no auto-switch. See FQA ``docs/FORWARD_PROTOCOL.md`` §2.
    """
    try:
        if not settings.get_bool("quant_forward_enabled", True):
            return {"ok": True, "skipped": "前向候选未启用（quant_forward_enabled=false）"}
        proc = _ensure_pit_db_or_fail() or quant_manager.run_project_command(
            "python scripts/forward_candidate.py daily", timeout=3600
        )
        ok = proc.get("returncode") == 0
        db.log_operation(
            "quant_forward_candidate", {},
            {"ok": ok, "tail": (proc.get("stdout") or "")[-200:]},
        )
        return {"ok": ok}
    except Exception as exc:  # noqa: BLE001
        logger.exception("forward candidate run failed")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def run_forward_health() -> dict[str, Any]:
    """Saturday — evaluate the forward RISK gate (not a capability gate).

    ``scripts/forward_health.py`` exits 0 when every hard gate passes and 1 when
    one fails; both are VALID results (a failed gate is information, not a job
    error), so the job reports ``ok`` for either and surfaces the verdict.
    """
    try:
        proc = _ensure_pit_db_or_fail() or quant_manager.run_project_command(
            "python scripts/forward_health.py", timeout=7200
        )
        rc = proc.get("returncode")
        verdict = None
        try:
            root = Path(settings.get("quant_root", ""))
            artifact = quant_manager._read_output_json(  # noqa: SLF001 — same package helper
                root, ("outputs/forward/forward_health.json",)
            )
            if isinstance(artifact, dict):
                verdict = (artifact.get("gate") or {}).get("verdict")
        except Exception:  # noqa: BLE001 — a missing/corrupt artifact must not fail the job
            verdict = None
        ok = rc in (0, 1)
        db.log_operation(
            "quant_forward_health", {},
            {"ok": ok, "returncode": rc, "verdict": verdict,
             "tail": (proc.get("stdout") or "")[-200:]},
        )
        return {"ok": ok, "verdict": verdict, "returncode": rc}
    except Exception as exc:  # noqa: BLE001
        logger.exception("forward health run failed")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def run_d_challenger_daily() -> dict[str, Any]:
    """Weekday 17:45 — advance the D-track model challenger (parallel shadow).

    Same D-track book/executor/intraday semantics, different ML scanner; its
    own ledger. Point-in-time like the main track. The promotion gate consumes
    this ledger on the first Sunday of each month.
    """
    try:
        if not settings.get_bool("quant_d_cycle_enabled", False):
            return {"ok": True, "skipped": "D 轨模型自优化循环未启用（quant_d_cycle_enabled=false）"}
        proc = _ensure_pit_db_or_fail() or quant_manager.run_project_command(
            "python cli.py dcycle challenger", timeout=3600
        )
        ok = proc.get("returncode") == 0
        db.log_operation(
            "quant_d_challenger", {},
            {"ok": ok, "tail": (proc.get("stdout") or "")[-200:]},
        )
        return {"ok": ok}
    except Exception as exc:  # noqa: BLE001
        logger.exception("d challenger run failed")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #
def run_shadow_daily() -> dict[str, Any]:
    """Run the single-track (D) shadow mode and email the daily report.

    ``shadow.accounts`` holds only ``D_5W`` after the 2026-09-08 A/B/C
    retirement, so the combined report is one 「## 账户 D_5W」 section; the loop
    still walks the config list so a re-added account would appear automatically.
    D is simulated (影子/模拟盘) — never described as live money.
    """
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
                email = mailer.send_mail(f"FQA D 轨影子日报 — {date}", report)
            elif not ok:
                email = mailer.send_mail(
                    "FQA D 轨影子日报运行失败", _failure_email_text("FQA D 轨影子日报运行失败", proc)
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
    _last_shadow_run = _stamp(result)
    return result


def run_calibration() -> dict[str, Any]:
    """Saturday slot — D-cycle cost-consistency audit (replaces the old §7 calibrate).

    The real A-share cost structure is regulatory-fixed (commission 2.5bp min5 /
    stamp 5bp sell / transfer 0.1bp): ``cli.py dcycle audit-cost`` verifies the
    configured model matches it and alerts on any accumulated deviation — no
    parameter tuning. No PIT database required.
    """
    global _last_calibration_run
    result: dict[str, Any] = {"ok": False}
    ws.publish("quant_calibration_started", {"ts": datetime.now().isoformat(timespec="seconds")})
    try:
        proc = quant_manager.run_project_command("python cli.py dcycle audit-cost", timeout=1800)
        ok = proc.get("returncode") == 0
        tail = (proc.get("stdout") or "")[-400:]

        email: dict[str, Any] = {"ok": False, "error": "auto-email disabled"}
        if settings.get_bool("quant_shadow_auto_email", True):
            if ok:
                email = mailer.send_mail("FQA 成本模型一致性检查（替代 §7 回校）", tail or "ok")
            else:
                email = mailer.send_mail(
                    "FQA 成本模型一致性检查失败",
                    _failure_email_text("FQA 成本模型一致性检查失败", proc),
                )

        result = {
            "ok": ok,
            "returncode": proc.get("returncode"),
            "detail": tail,
            "emailed": bool(email.get("ok")),
            "email": email,
        }
        db.log_operation("quant_calibration_run", {},
                         {k: v for k, v in result.items() if k not in ("detail", "email")})
        ws.publish("quant_calibrated", result)
    except Exception as exc:  # noqa: BLE001
        logger.exception("cost audit run failed")
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        ws.publish("quant_calibrated", result)
    _last_calibration_run = _stamp(result)
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

    if is_trading_day(now) and (now.hour, now.minute) >= (dh, dm):
        fire_once(
            "quant_catchup_daily",
            ("quant_autopilot_run", "quant_shadow_run", "quant_catchup_daily"),
            daily_job,
        )
    # Intraday feature rollup (15:02) and the D-track challenger (17:45) are
    # safe to catch up late: both are idempotent data/research steps that read
    # already-closed data. The preclose layer is deliberately NOT caught up —
    # its own time guard refuses to decide orders after the auction window
    # (never trade a past timestamp).
    if is_trading_day(now) and (now.hour, now.minute) >= (15, 2):
        fire_once(
            "quant_catchup_intraday",
            ("quant_intraday_refresh", "quant_catchup_intraday"),
            refresh_intraday_daily_job,
        )
    if is_trading_day(now) and (now.hour, now.minute) >= (17, 45):
        fire_once(
            "quant_catchup_challenger",
            ("quant_d_challenger", "quant_catchup_challenger"),
            run_d_challenger_daily,
        )
    # Depth is a LIVE intraday snapshot: only meaningful before the close.
    if is_trading_day(now) and (14, 40) <= (now.hour, now.minute) < (15, 10):
        fire_once(
            "quant_catchup_depth",
            ("quant_depth_snapshot", "quant_catchup_depth"),
            collect_depth_daily,
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
    # Forward-only live resume: whenever the backend (re)starts DURING a trading
    # session (computer reboot / app restart / backend crash), launch the live
    # trader for the remainder. ``cli.py live`` is idempotent via its pid lock and
    # only ever reads CURRENT prints, so a resume never trades a past timestamp.
    # The window mirrors the trader's own sessions — the 11:30-13:00 lunch break
    # is excluded (a 12:00 restart used to start a trader that just slept).
    in_session = (9, 30) <= (now.hour, now.minute) < (11, 30) or (13, 0) <= (now.hour, now.minute) < (15, 0)
    if is_trading_day(now) and in_session:
        db.log_operation(
            "quant_live_resume", {"date": now.strftime("%Y-%m-%d")}, {"launched": True}
        )
        threading.Thread(
            target=start_live_trader, daemon=True, name="quant-live-resume"
        ).start()


def live_watchdog() -> dict[str, Any]:
    """Every 5 minutes — relaunch the live trader if it died mid-session.

    Before this, a crashed trader was only noticed when the backend itself
    restarted (audit P-4: the "watchdog" in quant_manager is a log-file tailer,
    not a process monitor). The check is forward-only: the relaunched trader
    reads current prints and never back-fills a missed bar.
    """
    now = datetime.now()
    if not is_trading_day(now):
        return _stamp({"ok": True, "skipped": "not a trading day"})
    in_session = (9, 30) <= (now.hour, now.minute) < (11, 30) or (13, 0) <= (now.hour, now.minute) < (15, 0)
    if not in_session:
        return _stamp({"ok": True, "skipped": f"outside session ({now:%H:%M})"})
    if quant_manager.live_trader_alive():
        return _stamp({"ok": True, "alive": True})
    logger.warning("live trader not alive at %s — relaunching", now.strftime("%H:%M:%S"))
    db.log_operation("quant_live_watchdog", {"date": now.strftime("%Y-%m-%d")},
                     {"alive": False, "action": "relaunch"})
    result = start_live_trader()
    return _stamp({"ok": bool(result.get("ok")), "alive": False, "relaunch": result})


def collect_depth_daily() -> dict[str, Any]:
    """Weekday 14:40 AlphaFeed depth snapshot (live-execution layer dataset).

    Depth has no history, so this only accumulates a forward dataset — it does
    not touch the shadow ledger or any backtest number. Runs 10 minutes before
    the 14:50 preclose order decision so the two never contend.
    """
    try:
        proc = quant_manager.run_project_command("python scripts/collect_depth.py", timeout=600)
        db.log_operation("quant_depth_snapshot", {},
                         {"ok": proc.get("returncode") == 0, "tail": (proc.get("stdout") or "")[-200:]})
        return {"ok": proc.get("returncode") == 0}
    except Exception as exc:  # noqa: BLE001
        logger.exception("depth snapshot failed")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


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
            scheduler.add_job(
                _recording(DEPTH_JOB_ID, collect_depth_daily),
                # 14:40, NOT 14:50: the preclose order layer owns 14:50 and must
                # not be delayed by (or race) the depth snapshot.
                CronTrigger(day_of_week="mon-fri", hour=14, minute=40),
                id=DEPTH_JOB_ID, replace_existing=True,
                misfire_grace_time=3600, coalesce=True,
            )
            scheduler.add_job(
                _recording(LIVE_JOB_ID, start_live_trader),
                CronTrigger(day_of_week="mon-fri", hour=9, minute=25),
                id=LIVE_JOB_ID, replace_existing=True,
                misfire_grace_time=3600, coalesce=True,
            )
            scheduler.add_job(
                _recording(INTRA_JOB_ID, refresh_intraday_daily_job),
                CronTrigger(day_of_week="mon-fri", hour=15, minute=2),
                id=INTRA_JOB_ID, replace_existing=True,
                misfire_grace_time=3600, coalesce=True,
            )
            scheduler.add_job(
                _recording(PRECLOSE_JOB_ID, run_preclose_daily),
                CronTrigger(day_of_week="mon-fri", hour=14, minute=50),
                id=PRECLOSE_JOB_ID, replace_existing=True,
                # no long grace: a late preclose is refused by the time guard
                # inside run_preclose_daily (never decide orders retroactively)
                misfire_grace_time=60, coalesce=True,
            )
            scheduler.add_job(
                _recording(CHALLENGER_JOB_ID, run_d_challenger_daily),
                CronTrigger(day_of_week="mon-fri", hour=17, minute=45),
                id=CHALLENGER_JOB_ID, replace_existing=True,
                misfire_grace_time=3600, coalesce=True,
            )
            # Live-trader watchdog (audit P-4): every 5 minutes, relaunch the
            # trader if it died mid-session. The job itself is a no-op outside a
            # session or on a holiday.
            scheduler.add_job(
                _recording(WATCHDOG_JOB_ID, live_watchdog),
                IntervalTrigger(minutes=5),
                id=WATCHDOG_JOB_ID, replace_existing=True,
                misfire_grace_time=120, coalesce=True,
            )
            # Forward-period candidate shadow (FQA docs/FORWARD_PROTOCOL.md §2):
            # 15:20, after the 15:10 daily loop has settled the production book, so
            # both ledgers are advanced on the same data and the paired comparison
            # stays apples-to-apples.
            scheduler.add_job(
                _recording(FORWARD_CANDIDATE_JOB_ID, run_forward_candidate_daily),
                CronTrigger(day_of_week="mon-fri", hour=15, minute=20),
                id=FORWARD_CANDIDATE_JOB_ID, replace_existing=True,
                misfire_grace_time=3600, coalesce=True,
            )
            # Forward RISK gate (§1.3): weekly, with the replay-based tracking
            # error. Exit 1 = a hard gate failed, which is a RESULT, not an error.
            fh, fm = _parse_hhmm(settings.get("quant_forward_health_time"), (18, 30))
            scheduler.add_job(
                _recording(FORWARD_HEALTH_JOB_ID, run_forward_health),
                CronTrigger(day_of_week="sat", hour=fh, minute=fm),
                id=FORWARD_HEALTH_JOB_ID, replace_existing=True,
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


# --------------------------------------------------------------------------- #
# job catalog (panel 「任务调度」 card)
# --------------------------------------------------------------------------- #
#: Static schedule table — ``(job id, display name, cron label)`` in the order
#: the panel renders them. It is the fallback when the in-process scheduler is
#: not running (e.g. the backend was just started, or the scheduler failed to
#: start): the card must still list every job with its planned time, just with
#: ``next_run = None``. ``get_status`` overrides ``cron`` / ``next_run`` with the
#: live APScheduler values whenever the scheduler is up.
#:
#: The plan times mirror ``start_scheduler``'s ``CronTrigger`` arguments. Note
#: the weekday daily loop is *D-only* (autopilot/shadow over ``D_5W``) and the
#: Saturday/Sunday jobs are the D-cycle cost audit / monthly model cycle.
_JOB_SPECS: tuple[tuple[str, str, str], ...] = (
    (LIVE_JOB_ID, "实时模拟盘（09:25 启动）", "mon-fri 09:25"),
    (DEPTH_JOB_ID, "深度快照采集（14:40）", "mon-fri 14:40"),
    (PRECLOSE_JOB_ID, "收盘竞价委托（14:50）", "mon-fri 14:50"),
    (INTRA_JOB_ID, "盘中特征刷新（15:02）", "mon-fri 15:02"),
    (SHADOW_JOB_ID, "影子盘日报（15:10）", "mon-fri 15:10"),
    (CHALLENGER_JOB_ID, "D 轨挑战者（17:45）", "mon-fri 17:45"),
    (FORWARD_CANDIDATE_JOB_ID, "前向候选影子盘（15:20）", "mon-fri 15:20"),
    (WATCHDOG_JOB_ID, "实时盘看门狗（每 5 分钟）", "every 5m"),
    (FORWARD_HEALTH_JOB_ID, "前向风险闸门（周六 18:30）", "sat 18:30"),
    (CALIBRATE_JOB_ID, "成本模型一致性检查（周六 18:00）", "sat 18:00"),
    (WEEKLY_JOB_ID, "D 轨模型月度循环（周日 18:00）", "sun 18:00"),
)


def _job_catalog() -> list[dict[str, Any]]:
    """Return the static job table (pure: no settings, scheduler or I/O).

    Every entry carries the full panel schema so a caller never has to guess:
    ``{id, name, cron, next_run, last_run, last_status}``. ``next_run`` /
    ``last_run`` / ``last_status`` are ``None`` here — :func:`_job_entries`
    fills them from the live scheduler and the in-memory run records.
    """
    return [
        {
            "id": job_id,
            "name": name,
            "cron": cron,
            "next_run": None,
            "last_run": None,
            "last_status": None,
        }
        for job_id, name, cron in _JOB_SPECS
    ]


def _run_time(record: dict[str, Any] | None) -> str | None:
    """Best-effort run timestamp of a ``_last_*_run`` record (``None`` if absent)."""
    if not isinstance(record, dict):
        return None
    for key in ("ts", "last_run", "timestamp", "finished_at", "started_at"):
        value = record.get(key)
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value)).isoformat(timespec="seconds")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _run_status(record: dict[str, Any] | None) -> str | None:
    """Map a run record to ``ok`` / ``skipped`` / ``failed``.

    A job that returns ``{"ok": True, "skipped": ...}`` did NOT run — it must
    not be shown as 成功 (e.g. the D model-cycle jobs are intentionally gated
    off by ``quant_d_cycle_enabled=false``, see docs/D_MODEL_CYCLE.md §四).
    ``ok`` wins over ``returncode``; a record carrying neither stays ``None``
    so the panel shows「—」instead of inventing a status.
    """
    if not isinstance(record, dict):
        return None
    if record.get("skipped"):
        return "skipped"
    ok = record.get("ok")
    if isinstance(ok, bool):
        return "ok" if ok else "failed"
    returncode = record.get("returncode")
    if isinstance(returncode, int):
        return "ok" if returncode == 0 else "failed"
    return None


def _latest_record(*records: dict[str, Any] | None) -> dict[str, Any] | None:
    """Pick the newest of the candidate run records by their own timestamp.

    The daily slot runs ``autopilot`` *or* ``shadow`` depending on
    ``quant_autopilot_enabled``, and either can also be triggered manually from
    the panel — so both records are considered and the newer one wins.
    """
    best: dict[str, Any] | None = None
    best_ts: str | None = None
    for record in records:
        if not isinstance(record, dict):
            continue
        ts = _run_time(record)
        if ts is None:
            if best is None:
                best = record
            continue
        if best_ts is None or ts > best_ts:
            best, best_ts = record, ts
    return best


def _iso_time(value: Any) -> str | None:
    """ISO seconds string for a ``datetime`` (local wall clock), else ``None``."""
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is not None:
        value = value.replace(tzinfo=None)
    return value.isoformat(timespec="seconds")


def _trigger_cron(trigger: Any) -> str | None:
    """Render a live APScheduler trigger as ``"mon-fri 09:25"``.

    Falls back to ``str(trigger)`` for a non-cron trigger, and to ``None`` when
    the trigger is missing/unreadable (the caller then keeps the static label).
    """
    if trigger is None:
        return None
    values: dict[str, str] = {}
    for field in getattr(trigger, "fields", None) or ():
        name = getattr(field, "name", None)
        if name:
            values[str(name)] = str(field)
    if not values:
        return str(trigger) or None
    day_of_week = values.get("day_of_week", "*")
    hour = values.get("hour", "*")
    minute = values.get("minute", "*")
    if hour.isdigit() and minute.isdigit():
        return f"{day_of_week} {int(hour):02d}:{int(minute):02d}"
    return f"{day_of_week} {hour}:{minute}"


def _job_entries() -> list[dict[str, Any]]:
    """Merge the static catalog with the live scheduler + run records."""
    entries = _job_catalog()
    by_id = {entry["id"]: entry for entry in entries}

    # last_run / last_status from the in-memory records. Jobs that only write the
    # operation log record their own result in ``_last_job_runs`` so the panel's
    # 「任务调度」 card is not permanently「—」for live/depth/preclose/intraday/
    # challenger/watchdog (2026-09-09 audit).
    for job_id, record in (
        (SHADOW_JOB_ID, _latest_record(_last_autopilot_run, _last_shadow_run)),
        (CALIBRATE_JOB_ID, _last_calibration_run),
        (WEEKLY_JOB_ID, _last_weekly_run),
        (LIVE_JOB_ID, _last_job_runs.get(LIVE_JOB_ID)),
        (DEPTH_JOB_ID, _last_job_runs.get(DEPTH_JOB_ID)),
        (PRECLOSE_JOB_ID, _last_job_runs.get(PRECLOSE_JOB_ID)),
        (INTRA_JOB_ID, _last_job_runs.get(INTRA_JOB_ID)),
        (CHALLENGER_JOB_ID, _last_job_runs.get(CHALLENGER_JOB_ID)),
        (FORWARD_CANDIDATE_JOB_ID, _last_job_runs.get(FORWARD_CANDIDATE_JOB_ID)),
        (FORWARD_HEALTH_JOB_ID, _last_job_runs.get(FORWARD_HEALTH_JOB_ID)),
        (WATCHDOG_JOB_ID, _last_job_runs.get(WATCHDOG_JOB_ID)),
    ):
        entry = by_id.get(job_id)
        if entry is None:
            continue
        entry["last_run"] = _run_time(record)
        entry["last_status"] = _run_status(record)

    scheduler = _scheduler
    if scheduler is not None:
        try:
            jobs = list(scheduler.get_jobs())
        except Exception:  # noqa: BLE001 — a dead scheduler must not break the panel
            logger.warning("scheduler.get_jobs() failed; falling back to the static table")
            jobs = []
        for job in jobs:
            entry = by_id.get(str(getattr(job, "id", "")))
            if entry is None:
                continue
            cron = _trigger_cron(getattr(job, "trigger", None))
            if cron:
                entry["cron"] = cron
            entry["next_run"] = _iso_time(getattr(job, "next_run_time", None))
    return entries


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
        # Full job list for the panel's 「任务调度」 card: 8 rows even when the
        # scheduler is down (static plan times, next_run=None).
        "jobs": _job_entries(),
    }

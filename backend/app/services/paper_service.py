"""Daily AI paper recommendation, literature review, and email orchestration.

Orchestrates the whole "research desk" flow:

* every day: crawl arXiv + Semantic Scholar, score/select 20 papers, write a
  date-organised paper library under the desktop ``papers/`` folder, generate a
  Chinese literature review via the LLM, and email it;
* every month: summarise the previous month's papers into a hotspot/breakthrough
  report, written to ``papers/<YYYY-MM>/monthly_report.md`` and emailed.

Scheduling mirrors :mod:`app.services.storage_analysis` (APScheduler
``BackgroundScheduler`` + ``CronTrigger``), registered in ``main.py``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import db, ws
from app.config import settings
from app.services import mailer, paper_crawler, task_manager
from app.services.llm_client import LLMClient

logger = logging.getLogger(__name__)

DAILY_JOB_ID = "paper_daily"
MONTHLY_JOB_ID = "paper_monthly"

_scheduler: BackgroundScheduler | None = None
_scheduler_started = False
_scheduler_lock = threading.Lock()

_SYSTEM = (
    "你是资深的 AI 领域科研助理，擅长撰写中文文献综述。"
    "请始终用简体中文、Markdown 格式输出，内容准确、条理清晰。"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _papers_dir() -> Path:
    return Path(settings.get_papers_dir() or str(Path.home() / "Desktop" / "papers"))


def _slug(title: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9一-鿿]+", "-", title).strip("-").lower()
    return (s or "paper")[:60]


def _parse_hhmm(value: Any, default: tuple[int, int]) -> tuple[int, int]:
    try:
        hh, mm = str(value).strip().split(":")
        h, m = int(hh), int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except Exception:  # noqa: BLE001
        pass
    return default


def _previous_month() -> str:
    return (datetime.now().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")


def _paper_line(p: dict[str, Any], idx: int) -> str:
    meta = ", ".join(
        x
        for x in (
            p.get("source") or "",
            p.get("categories") or p.get("fields") or "",
            p.get("venue") or "",
            f"引用{p.get('citation_count', 0)}",
        )
        if x
    )
    abstract = (p.get("abstract") or "").strip()[:600]
    return f"{idx}. 《{p.get('title', '')}》\n   元信息：{meta}\n   摘要：{abstract or '（无摘要）'}\n"


# ---------------------------------------------------------------------------
# Report generation (LLM + deterministic fallback)
# ---------------------------------------------------------------------------
async def _llm_chat(prompt: str) -> str:
    result = await LLMClient().chat(
        [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": prompt}]
    )
    return str(result.get("content") or "").strip()


def _build_daily_prompt(frontier: list[dict[str, Any]], top: list[dict[str, Any]]) -> str:
    lines = ["以下是今日推荐的两组 AI 论文。", "", "## 本月前沿论文", ""]
    for i, p in enumerate(frontier, 1):
        lines.append(_paper_line(p, i))
    lines += ["", "## 近一年高分论文", ""]
    for i, p in enumerate(top, 1):
        lines.append(_paper_line(p, i))
    lines += [
        "",
        "请基于以上论文撰写一篇中文文献综述，包含：",
        "1. 总体概览（本日论文覆盖的研究方向）；",
        "2. 分主题梳理各论文的核心贡献与相互联系；",
        "3. 值得关注的技术趋势与潜在突破；",
        "4. 每篇论文一句话总结。",
        "用 Markdown 输出，标题以「# 每日 AI 论文综述」开头。",
    ]
    return "\n".join(lines)


def _fallback_daily_report(frontier: list[dict[str, Any]], top: list[dict[str, Any]]) -> str:
    def section(title: str, papers: list[dict[str, Any]]) -> list[str]:
        out = [f"## {title}", ""]
        for i, p in enumerate(papers, 1):
            meta = ", ".join(
                x
                for x in (p.get("venue") or "", f"引用{p.get('citation_count', 0)}", p.get("source") or "")
                if x
            )
            out.append(f"{i}. **{p.get('title', '')}**")
            if meta:
                out.append(f"   - {meta}")
            abstract = (p.get("abstract") or "").strip()
            if abstract:
                out.append(f"   - 摘要：{abstract[:300]}{'…' if len(abstract) > 300 else ''}")
        out.append("")
        return out

    lines = ["# 每日 AI 论文综述", "", f"- 日期：{datetime.now().strftime('%Y-%m-%d')}", ""]
    lines += section("本月前沿论文", frontier)
    lines += section("近一年高分论文", top)
    lines += ["## 综述", "", "（LLM 生成失败，以上为自动整理的论文清单，稍后可重新运行以获取完整综述。）"]
    return "\n".join(lines)


def _generate_daily_report(
    frontier: list[dict[str, Any]], top: list[dict[str, Any]]
) -> str:
    try:
        content = asyncio.run(_llm_chat(_build_daily_prompt(frontier, top)))
        if content:
            return content
    except Exception:  # noqa: BLE001
        logger.exception("LLM daily report failed; using fallback")
    return _fallback_daily_report(frontier, top)


def _build_monthly_prompt(papers: list[dict[str, Any]], month: str) -> str:
    counter: Counter[str] = Counter()
    for p in papers:
        for c in (p.get("categories") or "").split(","):
            c = c.strip()
            if c:
                counter[c] += 1
    top_papers = sorted(papers, key=lambda p: p.get("score") or 0, reverse=True)[:50]

    lines = [f"以下是 {month} 当月抓取的 {len(papers)} 篇 AI 论文。", "", "## 分类统计", ""]
    for c, n in counter.most_common(20):
        lines.append(f"- {c}: {n}")
    lines += ["", "## 高分论文（前 50）", ""]
    for i, p in enumerate(top_papers, 1):
        lines.append(_paper_line(p, i))
    lines += ["", f"请总结 {month} 月的 AI 科研热点与突破，输出中文 Markdown 报告，标题以「# AI 科研月度热点与突破」开头。"]
    return "\n".join(lines)


def _fallback_monthly_report(papers: list[dict[str, Any]], month: str) -> str:
    counter: Counter[str] = Counter()
    for p in papers:
        for c in (p.get("categories") or "").split(","):
            c = c.strip()
            if c:
                counter[c] += 1
    top = sorted(papers, key=lambda p: p.get("score") or 0, reverse=True)[:20]

    lines = ["# AI 科研月度热点与突破", "", f"- 月份：{month}", f"- 论文总数：{len(papers)}", "", "## 分类统计", ""]
    for c, n in counter.most_common(20):
        lines.append(f"- {c}: {n}")
    lines += ["", "## 高分论文 Top 20", ""]
    for i, p in enumerate(top, 1):
        lines.append(f"{i}. **{p.get('title', '')}**（引用 {p.get('citation_count', 0)}，得分 {p.get('score', 0):.2f}）")
    lines += ["", "## 综述", "", "（LLM 生成失败，以上为自动统计，稍后可重新运行以获取完整月度总结。）"]
    return "\n".join(lines)


def _generate_monthly_report(papers: list[dict[str, Any]], month: str) -> str:
    try:
        content = asyncio.run(_llm_chat(_build_monthly_prompt(papers, month)))
        if content:
            return content
    except Exception:  # noqa: BLE001
        logger.exception("LLM monthly report failed; using fallback")
    return _fallback_monthly_report(papers, month)


# ---------------------------------------------------------------------------
# Persistence + file writing
# ---------------------------------------------------------------------------
def _paper_markdown(p: dict[str, Any]) -> str:
    lines = [
        f"# {p.get('title', '')}",
        "",
        f"- 作者：{p.get('authors') or '—'}",
        f"- 来源：{p.get('source') or '—'}",
        f"- 分类/领域：{p.get('categories') or p.get('fields') or '—'}",
        f"- 发布：{p.get('published_at') or '—'}",
        f"- 引用数：{p.get('citation_count', 0)}",
        f"- 会议/期刊：{p.get('venue') or '—'}",
        f"- 综合得分：{p.get('score', 0):.4f}",
        f"- 链接：{p.get('url') or '—'}",
        "",
        "## 摘要",
        "",
        (p.get("abstract") or "（无摘要）").strip(),
        "",
    ]
    return "\n".join(lines)


def _seen_paper_keys() -> set[str]:
    """Keys (arXiv id or lower-cased title) of every paper already recommended.

    The recommendation pool is filtered against this so a paper is never
    re-recommended on a later day — the "never repeat" guarantee.
    """
    keys: set[str] = set()
    for r in db.query("SELECT arxiv_id, title FROM papers"):
        keys.add(r["arxiv_id"] or (r["title"] or "").strip().lower())
    return keys


def _persist_papers(papers: list[dict[str, Any]], crawl_date: str) -> None:
    for p in papers:
        if p.get("arxiv_id"):
            db.execute(
                "INSERT OR IGNORE INTO papers(arxiv_id, title, authors, abstract, categories, "
                "fields, published_at, url, source, citation_count, venue, score, set_tag, crawl_date) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    p.get("arxiv_id"),
                    p.get("title", ""),
                    p.get("authors", ""),
                    p.get("abstract", ""),
                    p.get("categories", ""),
                    p.get("fields", ""),
                    p.get("published_at", ""),
                    p.get("url", ""),
                    p.get("source", ""),
                    int(p.get("citation_count", 0)),
                    p.get("venue", ""),
                    float(p.get("score", 0)),
                    p.get("set_tag", ""),
                    crawl_date,
                ),
            )
        elif db.query_one("SELECT id FROM papers WHERE title = ?", (p.get("title", ""),)) is None:
            db.execute(
                "INSERT INTO papers(arxiv_id, title, authors, abstract, categories, "
                "fields, published_at, url, source, citation_count, venue, score, set_tag, crawl_date) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    None,
                    p.get("title", ""),
                    p.get("authors", ""),
                    p.get("abstract", ""),
                    p.get("categories", ""),
                    p.get("fields", ""),
                    p.get("published_at", ""),
                    p.get("url", ""),
                    p.get("source", ""),
                    int(p.get("citation_count", 0)),
                    p.get("venue", ""),
                    float(p.get("score", 0)),
                    p.get("set_tag", ""),
                    crawl_date,
                ),
            )


def _write_daily_files(
    papers: list[dict[str, Any]], report: str, date_str: str
) -> Path:
    day_dir = _papers_dir() / date_str
    day_dir.mkdir(parents=True, exist_ok=True)

    index = [
        {k: p.get(k) for k in (
            "arxiv_id", "title", "authors", "abstract", "categories", "fields",
            "published_at", "url", "source", "citation_count", "venue", "score", "set_tag",
        )}
        for p in papers
    ]
    (day_dir / "papers.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for i, p in enumerate(papers, 1):
        fname = f"{i:02d}-{_slug(p.get('title', ''))}.md"
        (day_dir / fname).write_text(_paper_markdown(p), encoding="utf-8")
    (day_dir / "report.md").write_text(report, encoding="utf-8")
    return day_dir


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
def run_daily() -> dict[str, Any]:
    """Crawl, score, select, persist, report, and email — the daily pipeline."""
    try:
        task_manager.update_task(progress=0.05)
        categories = str(settings.get("arxiv_categories", ""))
        arxiv_papers = paper_crawler.fetch_arxiv(categories, since_days=365)
        task_manager.update_task(progress=0.3)
        s2_papers = paper_crawler.fetch_semantic_scholar(since_days=365)
        task_manager.update_task(progress=0.5)
        openalex_papers = paper_crawler.fetch_openalex(since_days=365)
        task_manager.update_task(progress=0.6)

        pool = paper_crawler.merge_dedupe(arxiv_papers, s2_papers, openalex_papers)
        # Never re-recommend a paper already shown on a previous day.
        pool = paper_crawler.filter_seen(pool, _seen_paper_keys())
        recs = paper_crawler.select_recommendations(pool)

        frontier = recs["frontier"]
        top = recs["top"]
        for p in frontier:
            p["score"] = round(paper_crawler.frontier_score(p), 4)
            p["set_tag"] = "frontier"
        for p in top:
            p["score"] = round(paper_crawler.top_score(p), 4)
            p["set_tag"] = "top"
        all_papers = frontier + top
        task_manager.update_task(progress=0.75)

        date_str = datetime.now().strftime("%Y-%m-%d")
        if all_papers:
            _persist_papers(all_papers, date_str)

        if all_papers:
            report = _generate_daily_report(frontier, top)
        else:
            report = (
                f"# 每日 AI 论文综述\n\n- 日期：{date_str}\n\n"
                "今日无新论文：已推荐的论文不再重复推荐，等待 arXiv 有新论文提交后自动恢复。\n"
            )
        day_dir = _write_daily_files(all_papers, report, date_str)
        report_id = db.execute(
            "INSERT INTO paper_reports(report_date, report_type, content, file_path, sent_to_email) "
            "VALUES (?, ?, ?, ?, 0)",
            (date_str, "daily", report, str(day_dir / "report.md")),
        )
        task_manager.update_task(progress=0.9)

        email_result: dict[str, Any] = {"ok": False, "error": "SMTP not configured"}
        if str(settings.get("smtp_host", "")).strip():
            email_result = mailer.send_mail(
                f"PAICC 每日 AI 论文综述 — {date_str}", report
            )
            if email_result.get("ok"):
                db.execute("UPDATE paper_reports SET sent_to_email = 1 WHERE id = ?", (report_id,))

        db.log_operation(
            "paper_daily_generated",
            {"date": date_str},
            {
                "report_id": report_id,
                "frontier": len(frontier),
                "top": len(top),
                "emailed": bool(email_result.get("ok")),
            },
        )
        ws.publish(
            "paper_digest",
            {
                "type": "daily",
                "date": date_str,
                "frontier": len(frontier),
                "top": len(top),
                "report_id": report_id,
                "emailed": bool(email_result.get("ok")),
            },
        )
        task_manager.update_task(progress=1.0)
        return {
            "ok": True,
            "date": date_str,
            "report_id": report_id,
            "frontier": len(frontier),
            "top": len(top),
            "folder": str(day_dir),
            "email": email_result,
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("daily paper pipeline failed")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def run_monthly() -> dict[str, Any]:
    """Summarise the previous month's papers into a hotspot/breakthrough report."""
    try:
        month = _previous_month()
        papers = db.query("SELECT * FROM papers WHERE crawl_date LIKE ?", (month + "%",))
        if not papers:
            return {"ok": True, "skipped": True, "month": month, "count": 0}

        report = _generate_monthly_report(papers, month)
        month_dir = _papers_dir() / month
        month_dir.mkdir(parents=True, exist_ok=True)
        fpath = month_dir / "monthly_report.md"
        fpath.write_text(report, encoding="utf-8")
        report_id = db.execute(
            "INSERT INTO paper_reports(report_date, report_type, content, file_path, sent_to_email) "
            "VALUES (?, ?, ?, ?, 0)",
            (month, "monthly", report, str(fpath)),
        )

        email_result: dict[str, Any] = {"ok": False, "error": "SMTP not configured"}
        if str(settings.get("smtp_host", "")).strip():
            email_result = mailer.send_mail(f"PAICC AI 科研月度总结 — {month}", report)
            if email_result.get("ok"):
                db.execute("UPDATE paper_reports SET sent_to_email = 1 WHERE id = ?", (report_id,))

        db.log_operation(
            "paper_monthly_generated",
            {"month": month},
            {"report_id": report_id, "count": len(papers), "emailed": bool(email_result.get("ok"))},
        )
        ws.publish(
            "paper_digest",
            {"type": "monthly", "month": month, "count": len(papers), "emailed": bool(email_result.get("ok"))},
        )
        return {
            "ok": True,
            "month": month,
            "count": len(papers),
            "report_id": report_id,
            "file": str(fpath),
            "email": email_result,
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("monthly paper summary failed")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
def start_scheduler() -> None:
    """Idempotently start the daily + monthly paper schedulers."""
    global _scheduler, _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        try:
            scheduler = BackgroundScheduler()
            dh, dm = _parse_hhmm(settings.get("paper_daily_time"), (9, 0))
            mh, mm = _parse_hhmm(settings.get("paper_monthly_time"), (9, 30))
            scheduler.add_job(
                run_daily, CronTrigger(hour=dh, minute=dm), id=DAILY_JOB_ID, replace_existing=True
            )
            scheduler.add_job(
                run_monthly, CronTrigger(day=1, hour=mh, minute=mm), id=MONTHLY_JOB_ID, replace_existing=True
            )
            scheduler.start()
            _scheduler = scheduler
            _scheduler_started = True
            logger.info(
                "paper scheduler started (daily %02d:%02d, monthly %02d:%02d)", dh, dm, mh, mm
            )
        except Exception:  # noqa: BLE001
            logger.exception("failed to start paper scheduler")


# ---------------------------------------------------------------------------
# Queries (used by the router)
# ---------------------------------------------------------------------------
def list_papers(crawl_date: str | None = None, set_tag: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    conds: list[str] = []
    params: list[Any] = []
    if crawl_date:
        conds.append("crawl_date = ?")
        params.append(crawl_date)
    if set_tag:
        conds.append("set_tag = ?")
        params.append(set_tag)
    sql = "SELECT * FROM papers"
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return db.query(sql, tuple(params))


def list_reports() -> list[dict[str, Any]]:
    return db.query(
        "SELECT id, report_date, report_type, file_path, sent_to_email "
        "FROM paper_reports ORDER BY id DESC"
    )


def get_report(report_id: int) -> dict[str, Any] | None:
    return db.query_one("SELECT * FROM paper_reports WHERE id = ?", (report_id,))


def send_report_email(report_id: int) -> dict[str, Any]:
    row = get_report(report_id)
    if row is None:
        return {"ok": False, "error": "Report not found"}
    kind = "月度总结" if row["report_type"] == "monthly" else "每日综述"
    subject = f"PAICC AI 论文{kind} — {row['report_date']}"
    result = mailer.send_mail(subject, row["content"])
    if result.get("ok"):
        db.execute("UPDATE paper_reports SET sent_to_email = 1 WHERE id = ?", (report_id,))
    db.log_operation("paper_report_emailed", {"report_id": report_id}, {"ok": bool(result.get("ok"))})
    return result


def get_status() -> dict[str, Any]:
    paper_count = db.query_one("SELECT COUNT(*) AS c FROM papers")
    report_count = db.query_one("SELECT COUNT(*) AS c FROM paper_reports")
    last_report = db.query_one(
        "SELECT report_date, report_type, sent_to_email FROM paper_reports ORDER BY id DESC LIMIT 1"
    )
    return {
        "papers_dir": str(_papers_dir()),
        "daily_time": str(settings.get("paper_daily_time", "09:00")),
        "monthly_time": str(settings.get("paper_monthly_time", "09:30")),
        "arxiv_categories": str(settings.get("arxiv_categories", "")),
        "paper_count": paper_count["c"] if paper_count else 0,
        "report_count": report_count["c"] if report_count else 0,
        "last_report": last_report,
    }

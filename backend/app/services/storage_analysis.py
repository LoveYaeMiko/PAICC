"""Storage analysis and report scheduling.

Composes a Markdown storage report (disk usage, top-N large files, duplicate-file
summary, cleanable items and cleanup suggestions) and schedules automatic
regeneration via APScheduler, optionally emailing the result when SMTP is configured.

The heavy file-scanning primitives live in :mod:`app.services.file_ops`. They are
imported lazily so this module still loads — and degrades gracefully — if the file-ops
service is not yet available (e.g. during parallel development).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import db, ws
from app.config import settings
from app.services import mailer

logger = logging.getLogger(__name__)

REPORT_JOB_ID = "storage_report"
VALID_SCHEDULES = ("weekly", "monthly", "off")

_scheduler: BackgroundScheduler | None = None
_scheduler_started = False
_scheduler_lock = threading.Lock()


def _file_ops() -> Any:
    """Lazily import the file-ops service; returns ``None`` when unavailable."""
    try:
        from app.services import file_ops

        return file_ops
    except ImportError:
        return None


def _human_size(value: float) -> str:
    """Format a byte count into a human-readable string."""
    value = float(value)
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    if idx == 0:
        return f"{int(value)} B"
    return f"{value:.2f} {units[idx]}"


def _default_root() -> Path:
    """Return the quant project root when it exists, otherwise the user home."""
    root = settings.get_quant_root()
    path = Path(root) if root else None
    if path and path.is_dir():
        return path
    return Path.home()


def _disk_usage() -> list[dict[str, Any]]:
    """Overall disk usage for each mounted partition."""
    out: list[dict[str, Any]] = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        out.append(
            {
                "device": part.device,
                "mountpoint": part.mountpoint,
                "fstype": part.fstype,
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
                "percent": usage.percent,
            }
        )
    return out


def _fallback_large_files(root: Path, threshold_mb: float, limit: int) -> list[dict[str, Any]]:
    """Minimal large-file scan used only when the file-ops service is missing."""
    threshold = threshold_mb * 1024 * 1024
    found: list[dict[str, Any]] = []
    try:
        for dirpath, _dirnames, filenames in _safe_walk(root):
            for name in filenames:
                path = Path(dirpath) / name
                try:
                    st = path.stat()
                except OSError:
                    continue
                if st.st_size >= threshold:
                    found.append({"path": str(path), "size": st.st_size, "modified": st.st_mtime})
    except Exception:  # noqa: BLE001
        logger.exception("fallback large-file scan failed")
    found.sort(key=lambda item: item["size"], reverse=True)
    return found[:limit]


def _safe_walk(root: Path):
    """Yield ``(dirpath, dirnames, filenames)`` without raising on permission errors."""
    import os

    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            yield dirpath, dirnames, filenames
    except Exception:  # noqa: BLE001
        return


def _render_report(
    disks: list[dict[str, Any]],
    large_files: list[dict[str, Any]],
    duplicates: list[dict[str, Any]],
    dup_count: int,
    dup_wasted: int,
    cleanable: list[dict[str, Any]],
    cleanable_total: int,
    root: Path,
    generated_at: float,
) -> str:
    """Compose the Markdown report body."""
    ts = datetime.fromtimestamp(generated_at).strftime("%Y-%m-%d %H:%M:%S")
    lines: list[str] = [
        "# 存储分析报告",
        "",
        f"- 生成时间：{ts}",
        f"- 扫描根目录：`{root}`",
        "",
        "## 1. 磁盘使用",
        "",
        "| 设备 | 挂载点 | 文件系统 | 总容量 | 已用 | 可用 | 使用率 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for d in disks:
        lines.append(
            "| {device} | {mount} | {fstype} | {total} | {used} | {free} | {percent:.1f}% |".format(
                device=d["device"],
                mount=d["mountpoint"],
                fstype=d.get("fstype", ""),
                total=_human_size(d["total"]),
                used=_human_size(d["used"]),
                free=_human_size(d["free"]),
                percent=d["percent"],
            )
        )

    lines += ["", "## 2. 大文件 Top 10（>= 500 MB）", ""]
    if large_files:
        lines += ["| 路径 | 大小 | 修改时间 |", "| --- | --- | --- |"]
        for f in large_files:
            modified = datetime.fromtimestamp(f.get("modified", 0)).strftime("%Y-%m-%d %H:%M:%S")
            lines.append(f"| `{f.get('path', '')}` | {_human_size(f.get('size', 0))} | {modified} |")
    else:
        lines.append("未发现超过阈值的大文件。")

    lines += ["", "## 3. 重复文件统计", ""]
    if duplicates:
        lines.append(f"- 重复文件组数：{len(duplicates)}")
        lines.append(f"- 重复文件总数：{dup_count}")
        lines.append(f"- 可回收空间：{_human_size(dup_wasted)}")
    else:
        lines.append("未发现重复文件（最小文件 10 MB）。")

    lines += ["", "## 4. 可清理项", ""]
    if cleanable:
        lines.append(f"- 可清理项数量：{len(cleanable)}")
        lines.append(f"- 可回收空间：{_human_size(cleanable_total)}")
        lines += ["", "| 分类 | 路径 | 大小 |", "| --- | --- | --- |"]
        for c in cleanable:
            lines.append(f"| {c.get('category', '')} | `{c.get('path', '')}` | {_human_size(c.get('size', 0))} |")
    else:
        lines.append("未发现可清理项。")

    lines += ["", "## 5. 清理建议", ""]
    suggestions: list[str] = []
    for d in disks:
        if d["percent"] >= 80:
            suggestions.append(f"- 磁盘 `{d['mountpoint']}` 使用率已达 {d['percent']:.1f}%，建议尽快清理。")
    if dup_wasted > 0:
        suggestions.append(f"- 删除重复文件可释放约 {_human_size(dup_wasted)} 空间。")
    if cleanable_total > 0:
        suggestions.append(f"- 清理临时/缓存文件可释放约 {_human_size(cleanable_total)} 空间。")
    if not suggestions:
        suggestions.append("- 磁盘空间充足，暂无需清理。")
    lines += suggestions

    lines.append("")
    return "\n".join(lines)


def generate_report() -> dict[str, Any]:
    """Generate a storage report, persist it and return its id/content/timestamp."""
    generated_at = time.time()
    root = _default_root()
    disks = _disk_usage()

    large_files: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    cleanable: list[dict[str, Any]] = []

    fo = _file_ops()
    if fo is not None:
        try:
            large_files = list(fo.scan_large_files(str(root), threshold_mb=500, limit=10) or [])
        except Exception:  # noqa: BLE001
            logger.exception("scan_large_files failed")
        try:
            duplicates = list(fo.find_duplicates(str(root), min_size_mb=10) or [])
        except Exception:  # noqa: BLE001
            logger.exception("find_duplicates failed")
        try:
            cleanable = list(fo.list_cleanable() or [])
        except Exception:  # noqa: BLE001
            logger.exception("list_cleanable failed")
    else:
        large_files = _fallback_large_files(root, 500, 10)

    dup_count = sum(int(g.get("count", 0)) for g in duplicates)
    dup_wasted = sum(int(g.get("size", 0)) * max(0, int(g.get("count", 0)) - 1) for g in duplicates)
    cleanable_total = sum(int(c.get("size", 0)) for c in cleanable)

    content = _render_report(
        disks, large_files, duplicates, dup_count, dup_wasted, cleanable, cleanable_total, root, generated_at
    )

    report_id = db.execute(
        "INSERT INTO reports(generated_at, content, sent_to_email) VALUES (?, ?, 0)",
        (generated_at, content),
    )

    db.log_operation(
        "storage_report_generated",
        {"root": str(root)},
        {
            "report_id": report_id,
            "large_files": len(large_files),
            "duplicate_files": dup_count,
            "wasted_bytes": dup_wasted,
            "cleanable_bytes": cleanable_total,
        },
    )
    ws.publish("storage_report", {"report_id": report_id, "generated_at": generated_at})

    return {"report_id": report_id, "content": content, "generated_at": generated_at}


def list_reports() -> list[dict[str, Any]]:
    """List stored reports, newest first."""
    return db.query("SELECT id, generated_at, content, sent_to_email FROM reports ORDER BY id DESC")


def get_report(report_id: int) -> dict[str, Any] | None:
    """Fetch a single report by id."""
    return db.query_one(
        "SELECT id, generated_at, content, sent_to_email FROM reports WHERE id = ?", (report_id,)
    )


def list_cleanable() -> list[dict[str, Any]]:
    """Return the list of cleanable items (delegates to file-ops)."""
    fo = _file_ops()
    if fo is None:
        return []
    try:
        return list(fo.list_cleanable() or [])
    except Exception:  # noqa: BLE001
        logger.exception("list_cleanable failed")
        return []


def clean_items(items: list[str]) -> dict[str, Any]:
    """Delete the given cleanable items (delegates to file-ops)."""
    fo = _file_ops()
    if fo is None:
        return {"ok": False, "error": "file_ops service unavailable", "removed": 0, "freed_bytes": 0}
    try:
        result = fo.clean_items(items) or {}
        return {"ok": True, **result}
    except Exception as exc:  # noqa: BLE001
        logger.exception("clean_items failed")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _scheduled_job() -> None:
    """APScheduler job: regenerate the report and email it when SMTP is configured."""
    try:
        report = generate_report()
        if str(settings.get("smtp_host", "")).strip():
            try:
                send_email(report.get("report_id"))
            except Exception:  # noqa: BLE001
                logger.exception("scheduled report email failed")
    except Exception:  # noqa: BLE001
        logger.exception("scheduled storage report failed")


def _configure_job(scheduler: BackgroundScheduler) -> None:
    """(Re)attach the report job to ``scheduler`` based on ``report_schedule``."""
    try:
        scheduler.remove_job(REPORT_JOB_ID)
    except Exception:  # noqa: BLE001
        pass

    schedule = str(settings.get("report_schedule", "weekly")).strip().lower()
    if schedule == "off":
        return
    trigger = (
        CronTrigger(day=1, hour=9, minute=0)
        if schedule == "monthly"
        else CronTrigger(day_of_week="mon", hour=9, minute=0)
    )
    scheduler.add_job(_scheduled_job, trigger, id=REPORT_JOB_ID, replace_existing=True)


def start_scheduler() -> None:
    """Idempotently start the report scheduler (weekly Monday 09:00 by default)."""
    global _scheduler, _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        try:
            scheduler = BackgroundScheduler()
            _configure_job(scheduler)
            scheduler.start()
            _scheduler = scheduler
            _scheduler_started = True
            logger.info("storage report scheduler started (schedule=%s)", settings.get("report_schedule", "weekly"))
        except Exception:  # noqa: BLE001
            logger.exception("failed to start storage report scheduler")


def set_schedule(schedule: str) -> dict[str, Any]:
    """Persist and apply a new report schedule: ``weekly`` | ``monthly`` | ``off``."""
    schedule = (schedule or "").strip().lower()
    if schedule not in VALID_SCHEDULES:
        raise ValueError(f"invalid schedule {schedule!r}; expected one of {VALID_SCHEDULES}")

    settings.set("report_schedule", schedule)
    with _scheduler_lock:
        if _scheduler is not None:
            try:
                _configure_job(_scheduler)
            except Exception:  # noqa: BLE001
                logger.exception("failed to reconfigure storage report scheduler")

    db.log_operation("storage_report_schedule_set", {"schedule": schedule}, {"ok": True})
    return {"ok": True, "schedule": schedule}


def send_email(report_id: int | None = None) -> dict[str, Any]:
    """Email the latest report (or ``report_id``) via SMTP over SSL/STARTTLS."""
    host = str(settings.get("smtp_host", "")).strip()
    if not host:
        return {"ok": False, "error": "SMTP not configured"}

    row: dict[str, Any] | None
    if report_id is not None:
        row = db.query_one("SELECT * FROM reports WHERE id = ?", (report_id,))
    else:
        row = db.query_one("SELECT * FROM reports ORDER BY id DESC LIMIT 1")
    if row is None:
        return {"ok": False, "error": "No report available"}

    subject = f"PAICC 存储分析报告 — {datetime.fromtimestamp(row['generated_at']).strftime('%Y-%m-%d')}"
    result = mailer.send_mail(subject, row["content"])
    if not result.get("ok"):
        db.log_operation("storage_report_email_failed", {"report_id": row["id"]}, {"error": result.get("error")})
        return result

    db.execute("UPDATE reports SET sent_to_email = 1 WHERE id = ?", (row["id"],))
    db.log_operation("storage_report_emailed", {"report_id": row["id"]}, {"sent_to": result.get("sent_to")})
    return result

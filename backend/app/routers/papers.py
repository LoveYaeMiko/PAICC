"""Daily AI paper recommendation + literature review endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.services import paper_service, task_manager

router = APIRouter(prefix="/papers", tags=["papers"])


@router.post("/run")
def run() -> dict[str, Any]:
    """Trigger a manual daily crawl + report + email run in the background."""
    task_id = task_manager.start_task("paper_daily_run", paper_service.run_daily)
    return {"task_id": task_id, "status": "started"}


@router.get("")
def list_papers(
    crawl_date: str | None = None, set_tag: str | None = None, limit: int = 200
) -> list[dict[str, Any]]:
    """List crawled papers, optionally filtered by crawl date and recommendation set."""
    return paper_service.list_papers(crawl_date, set_tag, limit)


@router.get("/reports")
def list_reports() -> list[dict[str, Any]]:
    """List paper reports (daily + monthly), newest first."""
    return paper_service.list_reports()


@router.get("/reports/{report_id}")
def get_report(report_id: int) -> dict[str, Any]:
    """Fetch a single paper report by id."""
    report = paper_service.get_report(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return report


@router.post("/reports/{report_id}/send-email")
def send_email(report_id: int) -> dict[str, Any]:
    """Email a specific paper report."""
    return paper_service.send_report_email(report_id)


@router.get("/status")
def status() -> dict[str, Any]:
    """Return schedule, folder, and library statistics."""
    return paper_service.get_status()

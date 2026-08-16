"""Storage analysis report + cleanup endpoints."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import db, ws
from app.config import settings
from app.deps import require_confirmation
from app.services import storage_analysis

router = APIRouter(prefix="/storage", tags=["storage"])


class ScheduleRequest(BaseModel):
    schedule: Literal["weekly", "monthly", "off"]


class SendEmailRequest(BaseModel):
    report_id: int | None = None


class CleanRequest(BaseModel):
    items: list[str]
    confirmation_id: str | None = None


@router.post("/analysis")
def generate_report() -> dict[str, Any]:
    """Generate a storage analysis report and persist it."""
    return storage_analysis.generate_report()


@router.get("/reports")
def list_reports() -> list[dict[str, Any]]:
    """List historical storage reports, newest first."""
    return storage_analysis.list_reports()


@router.get("/reports/{report_id}")
def get_report(report_id: int) -> dict[str, Any]:
    """Fetch a single report by id."""
    report = storage_analysis.get_report(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return report


@router.get("/analysis/schedule")
def get_schedule() -> dict[str, Any]:
    """Return the current automatic report schedule (weekly / monthly / off)."""
    return {"schedule": settings.get("report_schedule", "weekly")}


@router.post("/analysis/schedule")
def set_schedule(req: ScheduleRequest) -> dict[str, Any]:
    """Set the automatic report schedule (weekly / monthly / off)."""
    try:
        return storage_analysis.set_schedule(req.schedule)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/send-email")
def send_email(req: SendEmailRequest) -> dict[str, Any]:
    """Email the latest (or a specific) storage report."""
    return storage_analysis.send_email(req.report_id)


@router.get("/cleanable-items")
def list_cleanable_items() -> list[dict[str, Any]]:
    """List cleanable temporary/cache items and their sizes."""
    return storage_analysis.list_cleanable()


@router.post("/clean")
def clean_items(req: CleanRequest) -> dict[str, Any]:
    """Delete the given cleanable items (requires an approved confirmation)."""
    require_confirmation(req.confirmation_id, action="clean")
    result = storage_analysis.clean_items(req.items)
    db.log_operation("storage_clean", {"items": req.items}, result)
    ws.publish("storage_cleaned", result)
    return result

"""Settings, confirmations and operation-log endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app import db
from app.config import DEFAULTS, settings
from app.models.schemas import ConfirmationRequest
from app.utils.confirmations import confirmations

router = APIRouter(tags=["meta"])


@router.get("/settings")
def get_settings() -> dict[str, Any]:
    return settings.all()


@router.put("/settings")
def put_settings(payload: dict[str, Any]) -> dict[str, Any]:
    updated: dict[str, Any] = {}
    for key, value in payload.items():
        if key in DEFAULTS:
            settings.set(key, value)
            updated[key] = settings.get(key)
    return {"ok": True, "updated": updated}


@router.get("/confirmations")
def list_confirmations() -> list[dict[str, Any]]:
    return confirmations.list_pending()


@router.post("/confirmations")
def create_confirmation(req: ConfirmationRequest) -> dict[str, Any]:
    return confirmations.create(req.action, req.title, req.details)


@router.post("/confirmations/{cid}/approve")
def approve_confirmation(cid: str) -> dict[str, Any]:
    if not confirmations.approve(cid):
        raise HTTPException(status_code=404, detail="Confirmation not found or no longer pending")
    db.log_operation("confirmation_approved", {"confirmation_id": cid}, {"status": "approved"})
    return {"ok": True}


@router.post("/confirmations/{cid}/deny")
def deny_confirmation(cid: str) -> dict[str, Any]:
    if not confirmations.deny(cid):
        raise HTTPException(status_code=404, detail="Confirmation not found or no longer pending")
    db.log_operation("confirmation_denied", {"confirmation_id": cid}, {"status": "denied"})
    return {"ok": True}


@router.get("/logs/operations")
def list_operations(limit: int = 200) -> list[dict[str, Any]]:
    return db.list_operations(limit)

"""Shared FastAPI dependencies / guards."""
from __future__ import annotations

from fastapi import HTTPException

from app.utils.confirmations import confirmations


def require_confirmation(confirmation_id: str | None) -> None:
    """Raise unless ``confirmation_id`` refers to an approved confirmation.

    Endpoints with "modify (confirm)" permission call this before acting.
    """
    if not confirmation_id:
        raise HTTPException(status_code=428, detail="Confirmation required")
    if not confirmations.is_approved(confirmation_id):
        raise HTTPException(status_code=403, detail="Operation not approved or expired")

"""Shared FastAPI dependencies / guards."""
from __future__ import annotations

from fastapi import HTTPException

from app.utils.confirmations import confirmations


def require_confirmation(confirmation_id: str | None, action: str | None = None) -> None:
    """Raise unless ``confirmation_id`` refers to an approved confirmation.

    Endpoints with "modify (confirm)" permission call this before acting. When
    ``action`` is supplied the confirmation must have been created for that action,
    and a successful check consumes it (single-use) so it cannot be replayed.
    """
    if not confirmation_id:
        raise HTTPException(status_code=428, detail="Confirmation required")
    if not confirmations.is_approved(confirmation_id, action=action):
        raise HTTPException(status_code=403, detail="Operation not approved or expired")
    confirmations.consume(confirmation_id)

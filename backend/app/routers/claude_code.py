"""Claude Code CLI integration endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.services import claude_code

router = APIRouter(prefix="/claude", tags=["claude_code"])


class SendRequest(BaseModel):
    message: str


@router.post("/start")
def start() -> dict[str, Any]:
    return claude_code.start_session()


@router.post("/send")
def send(payload: SendRequest) -> dict[str, Any]:
    return claude_code.send_message(payload.message)


@router.get("/status")
def status() -> dict[str, Any]:
    return claude_code.status()


@router.post("/stop")
def stop() -> dict[str, Any]:
    return claude_code.stop_session()

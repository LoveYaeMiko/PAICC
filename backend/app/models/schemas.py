"""Shared Pydantic schemas used across routers."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "0.1.0"
    python_version: str = ""
    uptime_seconds: float = 0.0


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str


class ChatRequest(BaseModel):
    messages: list[Message]
    use_tools: bool = True
    # When re-sending after a confirmation approval, pass the approved id here;
    # the tool dispatcher injects it into any confirmation-requiring tool call.
    confirmation_id: str | None = None


class ChatResponse(BaseModel):
    content: str
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    needs_confirmation: bool = False
    confirmation: dict[str, Any] | None = None


class ConfirmationRequest(BaseModel):
    action: str
    title: str
    details: dict[str, Any] = Field(default_factory=dict)


class OkResponse(BaseModel):
    ok: bool = True
    message: str = ""

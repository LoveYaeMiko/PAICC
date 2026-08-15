"""AI assistant — LLM chat with Function Calling orchestration."""
from __future__ import annotations

import inspect
import json
import logging
import time
from typing import Any

from fastapi import APIRouter

from app import db
from app.config import settings
from app.models.schemas import ChatRequest, ChatResponse
from app.services.llm_client import LLMClient
from app.tools import registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai", tags=["ai"])

#: Maximum number of tool-calling round trips before we give up and return text.
MAX_ITERATIONS = 8

_SYSTEM_PROMPT_BASE = (
    "You are PAICC, the Personal AI Command Center assistant.\n"
    "You help the user by calling tools to inspect and control their Windows system, "
    "files, apps, and quant research project.\n"
    "Safety rule: read-only actions may run directly, but any action that modifies "
    "the system (deleting files, cleaning, uninstalling, killing processes, running "
    "unknown scripts, changing settings, executing quant commands) requires explicit "
    "user approval and must go through the confirmation flow. When a tool asks for "
    "confirmation, stop and report it to the user instead of proceeding."
)


def _summarize_quant_status(status: Any) -> str:
    """Reduce the quant-manager status payload to a single short line."""
    if not status:
        return "unknown"
    if isinstance(status, dict):
        red = status.get("red_line") or status.get("red_lines") or status.get("alerts")
        if red:
            return json.dumps(red, ensure_ascii=False, default=str)
        if "status" in status:
            return str(status["status"])
        return json.dumps(status, ensure_ascii=False, default=str)[:200]
    return str(status)[:200]


async def _build_system_prompt() -> str:
    """Compose the system prompt: identity, safety rule, quant status, user prefs."""
    lines: list[str] = [_SYSTEM_PROMPT_BASE]

    # Brief quant red-line status (best-effort; the module may not exist yet).
    status = None
    try:
        from app.services import quant_manager

        fn = getattr(quant_manager, "get_status", None)
        if callable(fn):
            status = fn()
            if inspect.isawaitable(status):
                status = await status
    except Exception:  # noqa: BLE001
        logger.debug("quant status unavailable", exc_info=True)
        status = None
    lines.append(f"Quant red-line status: {_summarize_quant_status(status)}")

    # User preferences from settings.
    try:
        prefs = {
            "default_user": settings.get("default_user", "local"),
            "quant_root": settings.get_quant_root(),
        }
        lines.append(f"User preferences: {json.dumps(prefs, ensure_ascii=False)}")
    except Exception:  # noqa: BLE001
        pass

    return "\n".join(lines)


def _assistant_message(content: str, tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Rebuild an OpenAI-format assistant message carrying the tool calls."""
    return {
        "role": "assistant",
        "content": content or "",
        "tool_calls": [
            {
                "id": tc.get("id") or f"call_{i}",
                "type": "function",
                "function": {
                    "name": tc.get("name") or "",
                    "arguments": json.dumps(tc.get("arguments") or {}, ensure_ascii=False),
                },
            }
            for i, tc in enumerate(tool_calls)
        ],
    }


def _record_conversation(req: ChatRequest, assistant_content: str) -> None:
    """Best-effort persistence of the latest exchange into ``conversations``."""
    try:
        last = req.messages[-1].content if req.messages else ""
        if last:
            db.execute(
                "INSERT INTO conversations(role, content, created_at) VALUES (?, ?, ?)",
                ("user", last, time.time()),
            )
        if assistant_content:
            db.execute(
                "INSERT INTO conversations(role, content, created_at) VALUES (?, ?, ?)",
                ("assistant", assistant_content, time.time()),
            )
    except Exception:  # noqa: BLE001
        logger.debug("failed to record conversation", exc_info=True)


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    """Run the agentic chat loop, resolving tool calls with the registry."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": await _build_system_prompt()}
    ]
    messages.extend({"role": m.role, "content": m.content} for m in req.messages)

    tools = registry.get_tools() if req.use_tools else None
    collected: list[dict[str, Any]] = []
    result_log: dict[str, Any] = {"has_tools": req.use_tools}

    try:
        llm = LLMClient()
        content = ""
        for _ in range(MAX_ITERATIONS):
            resp = await llm.chat(messages, tools=tools)
            content = resp.get("content", "")
            tool_calls = resp.get("tool_calls") or []
            if not tool_calls:
                _record_conversation(req, content)
                return ChatResponse(content=content, tool_calls=collected)

            messages.append(_assistant_message(content, tool_calls))

            for tc in tool_calls:
                name = tc.get("name") or ""
                args = dict(tc.get("arguments") or {})
                meta = registry.get_meta(name)
                if req.confirmation_id and meta.get("require_confirmation"):
                    args["confirmation_id"] = req.confirmation_id

                result = await registry.dispatch(name, args)
                record = {"name": name, "arguments": args, "result": result}

                if result.get("needs_confirmation"):
                    collected.append(record)
                    return ChatResponse(
                        content="该操作需要确认",
                        tool_calls=collected,
                        needs_confirmation=True,
                        confirmation=result,
                    )

                collected.append(record)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id"),
                        "content": json.dumps(result, ensure_ascii=False, default=str),
                    }
                )

        _record_conversation(req, content)
        return ChatResponse(content=content, tool_calls=collected)
    except RuntimeError as exc:
        # Missing API key / provider errors — return a helpful message, not a 500.
        result_log["error"] = str(exc)
        return ChatResponse(content=str(exc), tool_calls=collected)
    except Exception as exc:  # noqa: BLE001
        logger.exception("ai chat failed")
        result_log["error"] = str(exc)
        return ChatResponse(
            content=f"AI 调用失败：{type(exc).__name__}: {exc}",
            tool_calls=collected,
        )
    finally:
        try:
            db.log_operation(
                "ai_chat", {"messages_count": len(req.messages)}, result_log
            )
        except Exception:  # noqa: BLE001
            pass


@router.get("/tools")
def list_tools() -> list[dict[str, Any]]:
    """List registered Function-Calling tools with their metadata."""
    out: list[dict[str, Any]] = []
    for t in registry.get_tools():
        fn = t.get("function") or {}
        name = fn.get("name") or ""
        meta = registry.get_meta(name)
        out.append(
            {
                "name": name,
                "description": fn.get("description", ""),
                "category": meta.get("category", "general"),
                "require_confirmation": bool(meta.get("require_confirmation", False)),
            }
        )
    return out

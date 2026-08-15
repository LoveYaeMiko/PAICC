"""Function-Calling tool registry + dispatcher.

Tools are declared with the ``@tool`` decorator using OpenAI's function-calling JSON
Schema shape. Handlers may be sync or async. Dangerous handlers set
``require_confirmation=True``; the dispatcher then returns a confirmation request instead
of executing, and the caller must re-invoke with a valid ``confirmation_id``.
"""
from __future__ import annotations

import inspect
import logging
from typing import Any, Callable

from app import db
from app.utils.confirmations import confirmations

logger = logging.getLogger(__name__)

#: OpenAI-format tool schemas exposed to the LLM.
TOOLS: list[dict[str, Any]] = []
_HANDLERS: dict[str, Callable[..., Any]] = {}
_META: dict[str, dict[str, Any]] = {}


def tool(
    name: str,
    description: str,
    parameters: dict[str, Any],
    *,
    require_confirmation: bool = False,
    category: str = "general",
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        if name in _HANDLERS:
            logger.warning("duplicate tool registration skipped: %s", name)
            return fn
        _HANDLERS[name] = fn
        _META[name] = {
            "require_confirmation": require_confirmation,
            "category": category,
        }
        TOOLS.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                },
            }
        )
        return fn

    return deco


def get_tools() -> list[dict[str, Any]]:
    return list(TOOLS)


def get_meta(name: str) -> dict[str, Any]:
    return _META.get(name, {})


async def dispatch(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Execute a tool by name. Returns the handler result, or a confirmation request."""
    fn = _HANDLERS.get(name)
    if fn is None:
        return {"ok": False, "error": f"unknown tool: {name}"}

    meta = _META.get(name, {})
    args = arguments or {}

    if meta.get("require_confirmation"):
        cid = args.get("confirmation_id")
        if not cid or not confirmations.is_approved(cid):
            action_title = args.get("_title") or name.replace("_", " ").title()
            safe_args = {k: v for k, v in args.items() if k not in ("confirmation_id", "_title")}
            req = confirmations.create(action=name, title=action_title, details=safe_args)
            db.log_operation("tool_confirmation_requested", {"tool": name, "args": safe_args}, {"status": "pending"})
            return {"needs_confirmation": True, **req}

    try:
        result = fn(**args)
        if inspect.isawaitable(result):
            result = await result
    except Exception as exc:  # noqa: BLE001
        logger.exception("tool %s failed", name)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    if isinstance(result, dict):
        result.setdefault("ok", True)
        return result
    return {"ok": True, "result": result}

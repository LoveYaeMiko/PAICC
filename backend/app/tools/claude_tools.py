"""Claude Code function-calling tools."""
from __future__ import annotations

from typing import Any

from app.services import claude_code
from app.tools.registry import tool

_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "description": "The task/instruction to send to Claude Code (e.g. run a script, edit code, run a backtest).",
        },
    },
    "required": ["task"],
}


@tool(
    name="claude_run_task",
    description="Start (if needed) a local Claude Code session and send it a task to execute.",
    parameters=_PARAMS,
    require_confirmation=True,
    category="claude",
)
def claude_run_task(
    task: str,
    confirmation_id: str | None = None,
    _title: str | None = None,
) -> dict[str, Any]:
    """Send a task to the local Claude Code CLI (auto-starts a session if needed).

    ``confirmation_id`` / ``_title`` are injected by the tool dispatcher on the
    post-approval re-invoke and are intentionally unused here.
    """
    res = claude_code.send_message(task)
    return {"ok": bool(res.get("ok")), "status": claude_code.status()}

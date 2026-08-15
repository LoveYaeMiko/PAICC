"""AI Function-Calling tools for the quant monitoring module."""
from __future__ import annotations

from typing import Any

from app.services import quant_manager
from app.tools.registry import tool


@tool(
    "get_quant_status",
    "读取量化项目 Phase 10 的四项红线状态（成本偏离、空腿偏差、Regime 切换、PEAD 异常）。",
    {"type": "object", "properties": {}},
    category="quant",
)
def get_quant_status() -> dict[str, Any]:
    """Return the current quant red-line status."""
    return quant_manager.get_status()


@tool(
    "list_quant_processes",
    "列出命令行中包含量化项目根目录的运行中进程（PID、CPU、内存、运行时长）。",
    {"type": "object", "properties": {}},
    category="quant",
)
def list_quant_processes() -> dict[str, Any]:
    """Return processes belonging to the quant project."""
    return {"processes": quant_manager.monitor_processes()}


@tool(
    "list_quant_commands",
    "列出量化项目预设的可执行命令（如模拟盘运行、回测等）。",
    {"type": "object", "properties": {}},
    category="quant",
)
def list_quant_commands() -> dict[str, Any]:
    """Return the preset quant commands."""
    return {"commands": quant_manager.list_commands()}


@tool(
    "run_quant_command",
    "在量化项目根目录执行一条 shell 命令（危险操作，需要用户确认）。",
    {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的 shell 命令，例如 python scripts/phase10_backtest.py"},
        },
        "required": ["command"],
    },
    require_confirmation=True,
    category="quant",
)
def run_quant_command(command: str, confirmation_id: str | None = None, **_extra: Any) -> dict[str, Any]:
    """Execute a command in the quant project root (confirmation gated by the dispatcher)."""
    return quant_manager.run_command(command=command)

"""Function-Calling tools for system monitoring and control."""
from __future__ import annotations

from typing import Any

from app.services import system_monitor
from app.tools.registry import tool


@tool(
    name="get_system_status",
    description="Get current system resource usage: CPU, memory, disk, network, GPU and uptime.",
    parameters={"type": "object", "properties": {}},
    category="system",
)
def get_system_status() -> dict[str, Any]:
    return system_monitor.get_system_stats()


@tool(
    name="list_processes",
    description="List running processes, sorted by CPU, memory or name.",
    parameters={
        "type": "object",
        "properties": {
            "sort": {
                "type": "string",
                "enum": ["cpu", "memory", "name"],
                "description": "Sort key (default: cpu).",
            },
        },
    },
    category="system",
)
def list_processes(sort: str = "cpu") -> list[dict[str, Any]]:
    return system_monitor.get_processes(sort_by=sort)


@tool(
    name="kill_process",
    description="Terminate a process by PID. Requires user confirmation.",
    parameters={
        "type": "object",
        "properties": {
            "pid": {"type": "integer", "description": "Process ID to terminate."},
        },
        "required": ["pid"],
    },
    require_confirmation=True,
    category="system",
)
def kill_process(
    pid: int,
    confirmation_id: str | None = None,
    _title: str | None = None,
) -> dict[str, Any]:
    return system_monitor.kill_process(pid)

"""System monitoring and control endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app import db
from app.deps import require_confirmation
from app.services import system_monitor

router = APIRouter(prefix="/system", tags=["system"])


class KillProcessRequest(BaseModel):
    pid: int
    confirmation_id: str | None = None


class PowerPlanRequest(BaseModel):
    name: str


class StartupToggleRequest(BaseModel):
    name: str
    enabled: bool


@router.get("/status")
def get_status() -> dict[str, Any]:
    """Return a snapshot of system resource usage."""
    return system_monitor.get_system_stats()


@router.get("/processes")
def get_processes(sort: str = "cpu") -> list[dict[str, Any]]:
    """Return up to 200 processes sorted by cpu/memory/name/disk."""
    return system_monitor.get_processes(sort_by=sort)


@router.post("/process/kill")
def kill_process(payload: KillProcessRequest) -> dict[str, Any]:
    """Terminate a process (requires an approved confirmation)."""
    require_confirmation(payload.confirmation_id, action="kill_process")
    result = system_monitor.kill_process(payload.pid)
    db.log_operation("kill_process", {"pid": payload.pid}, result)
    return result


@router.post("/process/{pid}/reveal")
def reveal_process(pid: int) -> dict[str, Any]:
    """Open Explorer with the process executable selected (logged, no confirmation)."""
    result = system_monitor.reveal_process(pid)
    db.log_operation("reveal_process", {"pid": pid}, result)
    return result


@router.get("/startup-items")
def list_startup_items() -> list[dict[str, Any]]:
    """List Windows startup items (registry Run keys + shell:startup folders)."""
    return system_monitor.list_startup_items()


@router.post("/startup/toggle")
def toggle_startup(payload: StartupToggleRequest) -> dict[str, Any]:
    """Enable/disable a startup item (logged; reversible, no confirmation)."""
    result = system_monitor.set_startup_item(payload.name, payload.enabled)
    db.log_operation(
        "set_startup_item", {"name": payload.name, "enabled": payload.enabled}, result
    )
    return result


@router.get("/power-plans")
def list_power_plans() -> list[dict[str, Any]]:
    """List Windows power plans."""
    return system_monitor.list_power_plans()


@router.post("/power-plan")
def set_power_plan(payload: PowerPlanRequest) -> dict[str, Any]:
    """Activate a Windows power plan (logged, no confirmation required)."""
    result = system_monitor.set_power_plan(payload.name)
    db.log_operation("set_power_plan", {"name": payload.name}, result)
    return result

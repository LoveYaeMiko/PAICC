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


@router.get("/status")
def get_status() -> dict[str, Any]:
    """Return a snapshot of system resource usage."""
    return system_monitor.get_system_stats()


@router.get("/processes")
def get_processes(sort: str = "cpu") -> list[dict[str, Any]]:
    """Return up to 200 processes sorted by cpu/memory/name."""
    return system_monitor.get_processes(sort_by=sort)


@router.post("/process/kill")
def kill_process(payload: KillProcessRequest) -> dict[str, Any]:
    """Terminate a process (requires an approved confirmation)."""
    require_confirmation(payload.confirmation_id)
    result = system_monitor.kill_process(payload.pid)
    db.log_operation("kill_process", {"pid": payload.pid}, result)
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

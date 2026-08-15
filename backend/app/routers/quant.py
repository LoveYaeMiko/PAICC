"""Quant project monitoring endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.deps import require_confirmation
from app.services import quant_manager

router = APIRouter(prefix="/quant", tags=["quant"])


class RegisterProjectRequest(BaseModel):
    name: str
    root_path: str
    config_file: str | None = None
    log_dir: str | None = None


class AddCommandRequest(BaseModel):
    project_id: int
    name: str
    command: str
    description: str = ""


class RunCommandRequest(BaseModel):
    command_id: int | None = None
    command: str | None = None
    confirmation_id: str | None = None


@router.get("/projects")
def list_projects() -> list[dict[str, Any]]:
    return quant_manager.list_projects()


@router.post("/projects")
def register_project(payload: RegisterProjectRequest) -> dict[str, Any]:
    return quant_manager.register_project(
        name=payload.name,
        root_path=payload.root_path,
        config_file=payload.config_file,
        log_dir=payload.log_dir,
    )


@router.get("/detect")
def detect(root_path: str | None = None) -> dict[str, Any]:
    target = root_path or settings.get_quant_root()
    if not target:
        raise HTTPException(status_code=400, detail="root_path is required")
    return quant_manager.detect_project(target)


@router.get("/status")
def status() -> dict[str, Any]:
    return quant_manager.get_status()


@router.get("/processes")
def processes() -> list[dict[str, Any]]:
    return quant_manager.monitor_processes()


@router.get("/commands")
def list_commands(project_id: int | None = None) -> list[dict[str, Any]]:
    return quant_manager.list_commands(project_id)


@router.post("/commands")
def add_command(payload: AddCommandRequest) -> dict[str, Any]:
    return quant_manager.add_command(
        project_id=payload.project_id,
        name=payload.name,
        command=payload.command,
        description=payload.description,
    )


@router.post("/command")
def run_command(payload: RunCommandRequest) -> dict[str, Any]:
    require_confirmation(payload.confirmation_id)
    try:
        return quant_manager.run_command(
            command_id=payload.command_id,
            command=payload.command,
            confirmation_id=payload.confirmation_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/logs")
def tail_log(lines: int = 100) -> list[str]:
    return quant_manager.tail_log(lines)

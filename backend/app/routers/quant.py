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
    dashboard_script: str | None = None
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


class StopCommandRequest(BaseModel):
    project_id: int | None = None
    confirmation_id: str | None = None


class SaveConfigRequest(BaseModel):
    project_id: int | None = None
    content: str
    confirmation_id: str | None = None


class SaveReportRequest(BaseModel):
    title: str
    content: str


@router.get("/projects")
def list_projects() -> list[dict[str, Any]]:
    return quant_manager.list_projects()


@router.post("/projects")
def register_project(payload: RegisterProjectRequest) -> dict[str, Any]:
    return quant_manager.register_project(
        name=payload.name,
        root_path=payload.root_path,
        config_file=payload.config_file,
        dashboard_script=payload.dashboard_script,
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
    require_confirmation(payload.confirmation_id, action="run_quant_command")
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


@router.post("/stop")
def stop_command(payload: StopCommandRequest) -> dict[str, Any]:
    require_confirmation(payload.confirmation_id, action="stop_quant_command")
    return quant_manager.stop_command(project_id=payload.project_id)


@router.get("/config")
def get_config(project_id: int | None = None) -> dict[str, Any]:
    try:
        return quant_manager.get_config_text(project_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/config")
def save_config(payload: SaveConfigRequest) -> dict[str, Any]:
    require_confirmation(payload.confirmation_id, action="save_quant_config")
    try:
        return quant_manager.save_config(payload.project_id, payload.content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/save-report")
def save_report(payload: SaveReportRequest) -> dict[str, Any]:
    """Save a report/result into the knowledge base."""
    return quant_manager.save_report_to_kb(payload.title, payload.content)


@router.get("/logs")
def tail_log(lines: int = 100) -> list[str]:
    return quant_manager.tail_log(lines)

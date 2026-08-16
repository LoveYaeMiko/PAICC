"""Application management endpoints: scan, list, launch, uninstall, favorite, icons."""
from __future__ import annotations

import asyncio
import os
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app import db
from app.deps import require_confirmation
from app.services import app_manager

router = APIRouter(prefix="/apps", tags=["apps"])


class StartRequest(BaseModel):
    id: int


class UninstallRequest(BaseModel):
    id: int
    confirmation_id: str | None = None


class FavoriteRequest(BaseModel):
    id: int
    is_favorite: bool


class PinRequest(BaseModel):
    path: str


@router.get("/list")
def list_apps(favorites_only: bool = False) -> list[dict[str, Any]]:
    return app_manager.list_apps(favorites_only)


@router.get("/recommend")
def recommend_apps(limit: int = 8) -> list[dict[str, Any]]:
    """Return the most-launched apps for the recommendation section."""
    return app_manager.recommend_apps(limit)


@router.post("/scan")
async def scan_apps() -> dict[str, Any]:
    """Scan Start Menu / Desktop shortcuts and registry uninstall entries."""
    return await asyncio.to_thread(app_manager.scan_apps)


@router.post("/start")
def start_app(req: StartRequest) -> dict[str, Any]:
    return app_manager.start_app(req.id)


@router.post("/uninstall")
def uninstall_app(req: UninstallRequest) -> dict[str, Any]:
    require_confirmation(req.confirmation_id, action="uninstall")
    return app_manager.uninstall_app(req.id, req.confirmation_id)


@router.post("/favorite")
def set_favorite(req: FavoriteRequest) -> dict[str, Any]:
    return app_manager.set_favorite(req.id, req.is_favorite)


@router.post("/pin")
async def pin_app(req: PinRequest) -> dict[str, Any]:
    """Register a dropped file (.lnk/.exe) as a favorite quick-launch app."""
    return await asyncio.to_thread(app_manager.pin_path, req.path)


@router.get("/icon/{app_id}")
async def get_icon(app_id: int) -> FileResponse:
    """Serve the cached icon, extracting it on demand if not yet cached."""
    row = db.query_one("SELECT path, icon_path FROM apps WHERE id = ?", (app_id,))
    if not row:
        raise HTTPException(status_code=404, detail="App not found")
    icon_path = row.get("icon_path") or ""
    if not (icon_path and os.path.exists(icon_path)):
        extracted = await asyncio.to_thread(
            app_manager.extract_icon, row.get("path") or "", app_id
        )
        icon_path = extracted or ""
    if icon_path and os.path.exists(icon_path):
        return FileResponse(icon_path, media_type="image/png")
    raise HTTPException(status_code=404, detail="Icon not found")

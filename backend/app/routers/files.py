"""File management endpoints: name search, content search, scan/duplicate tasks, open."""
from __future__ import annotations

import os
import platform
import subprocess
from typing import Any

from fastapi import APIRouter, HTTPException

from app import db
from app.services import everything, file_ops, task_manager, windows_search

router = APIRouter(prefix="/files", tags=["files"])


@router.get("/search")
def search(
    query: str,
    type: str | None = None,
    size_min: int | None = None,
    size_max: int | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    return everything.search(query, type=type, size_min=size_min, size_max=size_max, limit=limit)


@router.get("/content-search")
def content_search(query: str, top: int = 50) -> dict[str, Any]:
    return windows_search.search_content(query, top=top)


@router.post("/scan-large")
def scan_large(payload: dict[str, Any]) -> dict[str, Any]:
    directory = payload.get("directory") or ""
    threshold_mb = payload.get("threshold_mb", 100)
    if not directory or not os.path.isdir(directory):
        raise HTTPException(status_code=400, detail="Invalid directory")
    task_id = task_manager.start_task("scan_large_files", file_ops.scan_large_files, directory, threshold_mb)
    return {"task_id": task_id}


@router.post("/find-duplicates")
def find_duplicates(payload: dict[str, Any]) -> dict[str, Any]:
    directory = payload.get("directory") or ""
    min_size_mb = payload.get("min_size_mb", 1)
    if not directory or not os.path.isdir(directory):
        raise HTTPException(status_code=400, detail="Invalid directory")
    task_id = task_manager.start_task("find_duplicates", file_ops.find_duplicates, directory, min_size_mb)
    return {"task_id": task_id}


@router.get("/task/{task_id}")
def get_task(task_id: str) -> dict[str, Any]:
    task = task_manager.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.get("/folder-size")
def folder_size(directory: str) -> dict[str, Any]:
    if not directory or not os.path.isdir(directory):
        raise HTTPException(status_code=400, detail="Invalid directory")
    return {"directory": directory, "size": file_ops.folder_size(directory)}


@router.post("/open")
def open_path(payload: dict[str, Any]) -> dict[str, Any]:
    path = payload.get("path") or ""
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Path not found")
    try:
        if platform.system() == "Windows":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", path])
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    db.log_operation("file_open", {"path": path}, {"ok": True})
    return {"ok": True}

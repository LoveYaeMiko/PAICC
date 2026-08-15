"""File-system operations: large-file scanning, duplicate detection, cleanup, sizing.

The scan and duplicate functions are designed to run inside :mod:`app.services.task_manager`
tasks, so they emit progress via ``task_manager.update_task`` (a no-op when invoked
directly). Cleanup returns deterministic item ids so the frontend/AI can reference
items safely.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from typing import Any, Iterator

from app.services import task_manager

_HASH_READ_BYTES = 1024 * 1024  # hash the first 1 MB (plus size) for duplicate grouping


def _iter_files(directory: str) -> Iterator[str]:
    """Yield every regular-file path under ``directory``, tolerating errors."""
    try:
        for dirpath, dirnames, filenames in os.walk(directory):
            for fn in filenames:
                yield os.path.join(dirpath, fn)
    except OSError:
        return


def _count_files(directory: str) -> int:
    return sum(1 for _ in _iter_files(directory))


def scan_large_files(directory: str, threshold_mb: float = 100, limit: int = 500) -> list[dict[str, Any]]:
    """Walk ``directory`` and return files larger than ``threshold_mb``, sorted desc."""
    threshold = float(threshold_mb) * 1024 * 1024
    limit = int(limit)
    collected: list[dict[str, Any]] = []

    total = _count_files(directory)
    processed = 0
    for fp in _iter_files(directory):
        processed += 1
        try:
            st = os.stat(fp)
        except OSError:
            continue
        if st.st_size > threshold:
            collected.append({"path": fp, "size": st.st_size, "modified": st.st_mtime})
            if len(collected) >= limit:
                break
        if total and processed % 200 == 0:
            task_manager.update_task(progress=processed / total, result=collected)

    collected.sort(key=lambda x: x["size"], reverse=True)
    task_manager.update_task(progress=1.0, result=collected)
    return collected


def find_duplicates(directory: str, min_size_mb: float = 1, limit: int = 500) -> list[dict[str, Any]]:
    """Group files by exact size, then by sha256 of the first 1 MB, to find duplicates."""
    min_size = float(min_size_mb) * 1024 * 1024
    limit = int(limit)

    by_size: dict[int, list[str]] = {}
    total = _count_files(directory)
    processed = 0
    for fp in _iter_files(directory):
        processed += 1
        try:
            st = os.stat(fp)
        except OSError:
            continue
        if st.st_size >= min_size:
            by_size.setdefault(st.st_size, []).append(fp)
        if total and processed % 300 == 0:
            task_manager.update_task(progress=0.4 * processed / total)

    candidates: list[tuple[str, int]] = []
    for size, files in by_size.items():
        if len(files) > 1:
            candidates.extend((fp, size) for fp in files)

    if not candidates:
        task_manager.update_task(progress=1.0, result=[])
        return []

    grouped: dict[str, dict[str, Any]] = {}
    total2 = len(candidates)
    for i, (fp, size) in enumerate(candidates):
        digest = _partial_hash(fp, size)
        entry = grouped.setdefault(digest, {"size": size, "files": []})
        entry["files"].append(fp)
        if total2 and (i + 1) % 200 == 0:
            task_manager.update_task(progress=0.4 + 0.6 * (i + 1) / total2)

    results: list[dict[str, Any]] = []
    for digest, entry in grouped.items():
        if len(entry["files"]) > 1:
            results.append(
                {
                    "hash": digest,
                    "size": entry["size"],
                    "count": len(entry["files"]),
                    "files": entry["files"],
                }
            )
    results.sort(key=lambda g: g["size"] * (g["count"] - 1), reverse=True)
    results = results[:limit]
    task_manager.update_task(progress=1.0, result=results)
    return results


def _partial_hash(path: str, size: int) -> str:
    h = hashlib.sha256()
    h.update(str(size).encode("ascii"))
    try:
        with open(path, "rb") as fh:
            h.update(fh.read(_HASH_READ_BYTES))
    except OSError:
        pass
    return h.hexdigest()


def _cleanable_id(path: str) -> str:
    return hashlib.sha1(path.encode("utf-8", "ignore")).hexdigest()[:16]


def _cleanable_targets() -> list[dict[str, Any]]:
    """Return the canonical list of cleanable locations (shared by list/clean)."""
    home = os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
    system_root = os.environ.get("SystemRoot", r"C:\Windows")

    candidates: list[tuple[str, str, str]] = [
        ("temp", tempfile.gettempdir(), "User temporary files"),
        ("temp", os.path.join(system_root, "Temp"), "Windows temporary files"),
        ("cache", os.path.join(local, "Google", "Chrome", "User Data", "Default", "Cache"), "Chrome cache"),
        ("cache", os.path.join(local, "Microsoft", "Edge", "User Data", "Default", "Cache"), "Edge cache"),
        ("thumbnail", os.path.join(local, "Microsoft", "Windows", "Explorer"), "Explorer thumbnail cache"),
    ]

    items: list[dict[str, Any]] = []
    for category, path, description in candidates:
        if not os.path.exists(path):
            continue
        try:
            size = folder_size(path)
        except Exception:  # noqa: BLE001
            size = 0
        items.append(
            {
                "id": _cleanable_id(path),
                "category": category,
                "path": path,
                "size": size,
                "description": description,
                "is_dir": os.path.isdir(path),
            }
        )
    items.sort(key=lambda x: x["size"], reverse=True)
    return items


def list_cleanable() -> list[dict[str, Any]]:
    """List cleanable temp/cache locations with best-effort sizes."""
    return [
        {k: v for k, v in item.items() if k != "is_dir"}
        for item in _cleanable_targets()
    ]


def clean_items(item_ids: list[str]) -> dict[str, Any]:
    """Delete the given cleanable items (by id). Tolerates individual failures."""
    targets = {item["id"]: item for item in _cleanable_targets()}
    removed = 0
    freed = 0
    errors: list[dict[str, Any]] = []

    for iid in item_ids:
        item = targets.get(iid)
        if item is None:
            errors.append({"id": iid, "error": "unknown item"})
            continue
        path = item["path"]
        try:
            if item.get("is_dir"):
                shutil.rmtree(path)
            else:
                os.remove(path)
            removed += 1
            freed += int(item.get("size") or 0)
        except OSError as exc:
            errors.append({"id": iid, "path": path, "error": str(exc)})

    return {"removed": removed, "freed": freed, "errors": errors}


def folder_size(directory: str) -> int:
    """Recursively sum file sizes (skips symlinks and permission errors)."""
    total = 0
    try:
        for dirpath, dirnames, filenames in os.walk(directory):
            for fn in filenames:
                fp = os.path.join(dirpath, fn)
                try:
                    if os.path.islink(fp):
                        continue
                    total += os.path.getsize(fp)
                except OSError:
                    continue
    except OSError:
        pass
    return total

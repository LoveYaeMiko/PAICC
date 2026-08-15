"""File-name search via the Everything ``es.exe`` command-line tool.

Everything returns matching full paths extremely fast because it queries a pre-built
index. This module shells out to ``es.exe`` (path configured via the ``everything_path``
setting) and enriches each result with best-effort metadata from the filesystem.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

from app.config import settings


def search(
    query: str,
    type: str | None = None,
    size_min: int | None = None,
    size_max: int | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Search for files by name.

    Returns ``{"ok": True, "results": [{name, path, size, modified, is_dir}]}`` or
    ``{"ok": False, "error": ...}`` when the CLI is missing or fails.
    """
    es_path = str(settings.get("everything_path", "es.exe") or "es.exe")
    exe = shutil.which(es_path)
    if not exe and os.path.isabs(es_path) and os.path.isfile(es_path):
        exe = es_path
    if not exe:
        return {
            "ok": False,
            "error": "Everything CLI (es.exe) not found. Configure everything_path in Settings.",
        }

    args: list[str] = [exe, "-n", str(int(limit) if limit else 50), str(query)]
    if type:
        args.append(f"ext:{type}")
    if size_min is not None:
        args.append(f"size:>{int(size_min)}")
    if size_max is not None:
        args.append(f"size:<{int(size_max)}")

    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            timeout=15,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Everything search timed out."}
    except OSError as exc:
        return {"ok": False, "error": f"Failed to run Everything: {exc}"}

    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or "").strip() or "Everything returned an error."}

    results: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        path = line.strip()
        if not path:
            continue
        name = os.path.basename(path.rstrip("\\/"))
        is_dir = os.path.isdir(path)
        size = 0
        modified = None
        try:
            st = os.stat(path)
            size = st.st_size
            modified = st.st_mtime
        except OSError:
            # Best-effort metadata; tolerate deleted/renamed entries.
            pass
        results.append(
            {"name": name, "path": path, "size": size, "modified": modified, "is_dir": is_dir}
        )
    return {"ok": True, "results": results}

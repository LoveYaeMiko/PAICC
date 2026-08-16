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

#: Date-modified preset names -> Everything ``dm:`` filters.
_TIME_FILTERS: dict[str, str] = {
    "today": "dm:today",
    "week": "dm:lastweek",
    "month": "dm:lastmonth",
    "year": "dm:thisyear",
}

#: Semantic file-type names -> Everything ``ext:`` filter groups.
_TYPE_EXTS: dict[str, str] = {
    "image": "jpg;jpeg;png;gif;bmp;webp;svg;ico;tif;tiff",
    "video": "mp4;mkv;avi;mov;wmv;flv;webm;m4v",
    "audio": "mp3;wav;flac;aac;ogg;m4a;wma",
    "document": "pdf;doc;docx;xls;xlsx;ppt;pptx;txt;md;csv;json;rtf;odt",
    "archive": "zip;rar;7z;tar;gz;bz2;xz;iso",
    "executable": "exe;msi;bat;cmd;com;dll",
}


def _type_filter(type_: str) -> str:
    """Map a semantic type name to an Everything ``ext:`` filter (raw ext passes through)."""
    key = (type_ or "").strip().lower()
    if key in _TYPE_EXTS:
        return f"ext:{_TYPE_EXTS[key]}"
    return f"ext:{type_}"


def search(
    query: str,
    type: str | None = None,
    size_min: int | None = None,
    size_max: int | None = None,
    limit: int = 50,
    time: str | None = None,
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
        args.append(_type_filter(type))
    if size_min is not None:
        args.append(f"size:>{int(size_min)}mb")
    if size_max is not None:
        args.append(f"size:<{int(size_max)}mb")
    if time:
        dm = _TIME_FILTERS.get((time or "").strip().lower())
        if dm:
            args.append(dm)

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

"""Content search.

Prefers the native Windows Search index (via the ADO OLE DB provider exposed through
``pywin32``), which is fast and covers Office/PDF/plain-text documents. If ``pywin32``
is unavailable or the query fails, we fall back to a bounded recursive substring scan
over a small set of user/quant roots.
"""
from __future__ import annotations

import os
from typing import Any

from app.config import settings

#: Extensions the fallback scanner will actually read.
_TEXT_EXTS = {".txt", ".md", ".csv", ".json", ".py", ".yaml", ".yml", ".log"}

_FALLBACK_MAX_FILES = 2000
_FALLBACK_MAX_DEPTH = 4
_READ_CAP = 2_000_000  # bytes


def search_content(query: str, top: int = 50) -> dict[str, Any]:
    """Return ``{ok, results:[{path, snippet}]}`` for the given phrase."""
    query = (query or "").strip()
    if not query:
        return {"ok": True, "results": []}
    try:
        results = _search_windows(query, top)
        if results is not None:
            return {"ok": True, "results": results}
    except Exception:  # noqa: BLE001
        pass
    return _search_fallback(query, top)


def _search_windows(query: str, top: int) -> list[dict[str, Any]] | None:
    """Query the Windows Search index; returns ``None`` if unavailable."""
    try:
        import win32com.client  # type: ignore
    except ImportError:
        return None

    try:
        conn = win32com.client.Dispatch("ADODB.Connection")
        rs = win32com.client.Dispatch("ADODB.Recordset")
        conn.Open("Provider=Search.CollatorDSO;Extended Properties='Application=Windows';")
        safe = query.replace("'", "''")
        sql = (
            "SELECT System.ItemPathDisplay, System.Search.AutoSummary FROM SystemIndex "
            f"WHERE FREETEXT('{safe}') ORDER BY System.Search.Rank DESC"
        )
        rs.Open(sql, conn)
        results: list[dict[str, Any]] = []
        while not rs.EOF and len(results) < top:
            path = rs.Fields("System.ItemPathDisplay").Value
            snippet = ""
            try:
                snippet = rs.Fields("System.Search.AutoSummary").Value or ""
            except Exception:  # noqa: BLE001
                snippet = ""
            results.append({"path": path, "snippet": snippet})
            rs.MoveNext()
        rs.Close()
        conn.Close()
        return results
    except Exception:  # noqa: BLE001
        return None


def _search_fallback(query: str, top: int) -> dict[str, Any]:
    ql = query.lower()
    matches: list[dict[str, Any]] = []
    files_seen = 0

    for root in _fallback_roots():
        for dirpath, dirnames, filenames in os.walk(root):
            depth = dirpath[len(root):].count(os.sep)
            if depth >= _FALLBACK_MAX_DEPTH:
                dirnames[:] = []
            for fn in filenames:
                if os.path.splitext(fn)[1].lower() not in _TEXT_EXTS:
                    continue
                files_seen += 1
                if files_seen > _FALLBACK_MAX_FILES:
                    return {"ok": True, "results": matches[:top]}
                fp = os.path.join(dirpath, fn)
                text = _read_text(fp)
                if text is None:
                    continue
                idx = text.lower().find(ql)
                if idx != -1:
                    matches.append({"path": fp, "snippet": _make_snippet(text, idx, len(query))})
                    if len(matches) >= top:
                        return {"ok": True, "results": matches[:top]}
    return {"ok": True, "results": matches[:top]}


def _fallback_roots() -> list[str]:
    home = os.path.expanduser("~")
    roots = [os.path.join(home, sub) for sub in ("Documents", "Desktop", "Downloads")]
    quant_root = settings.get_quant_root()
    if quant_root:
        roots.append(str(quant_root))
    return [r for r in roots if os.path.isdir(r)]


def _read_text(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            return fh.read(_READ_CAP)
    except OSError:
        return None


def _make_snippet(text: str, idx: int, qlen: int, width: int = 120) -> str:
    start = max(0, idx - (width - qlen) // 2)
    end = start + width
    snippet = text[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet += "…"
    return snippet.replace("\n", " ").replace("\r", " ")

"""Function-calling tools for the file management module."""
from __future__ import annotations

from typing import Any

from app.services import everything, file_ops, windows_search
from app.tools.registry import tool


@tool(
    "search_files",
    "Search for files by name using the Everything index. Optionally filter by file "
    "extension and limit the number of results.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "File-name search query."},
            "type": {"type": "string", "description": "Optional file extension to filter by, e.g. 'pdf'."},
            "limit": {"type": "integer", "description": "Maximum number of results (default 50)."},
        },
        "required": ["query"],
    },
    category="files",
)
def search_files(query: str, type: str | None = None, limit: int = 50) -> dict[str, Any]:
    return everything.search(query, type=type, limit=limit)


@tool(
    "search_file_content",
    "Search inside text documents and files for a phrase using Windows Search (with a "
    "fallback recursive scan). Returns matching file paths with a short snippet.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Phrase to search for inside file contents."},
        },
        "required": ["query"],
    },
    category="files",
)
def search_file_content(query: str) -> dict[str, Any]:
    return windows_search.search_content(query)


@tool(
    "scan_large_files",
    "Scan a directory for large files above a size threshold, returning the largest first.",
    {
        "type": "object",
        "properties": {
            "directory": {"type": "string", "description": "Directory path to scan."},
            "threshold_mb": {"type": "number", "description": "Minimum file size in MB (default 100)."},
        },
        "required": ["directory"],
    },
    category="files",
)
def scan_large_files(directory: str, threshold_mb: float = 100) -> dict[str, Any]:
    results = file_ops.scan_large_files(directory, threshold_mb=threshold_mb)
    return {"results": results, "count": len(results)}


@tool(
    "find_duplicate_files",
    "Find duplicate files in a directory by comparing size and a hash of the first 1 MB.",
    {
        "type": "object",
        "properties": {
            "directory": {"type": "string", "description": "Directory path to scan for duplicates."},
        },
        "required": ["directory"],
    },
    category="files",
)
def find_duplicate_files(directory: str) -> dict[str, Any]:
    results = file_ops.find_duplicates(directory)
    return {"results": results, "count": len(results)}


@tool(
    "list_cleanable_files",
    "List cleanable temporary and cache files (user/Windows temp, browser caches, "
    "thumbnail cache) with their sizes and ids.",
    {
        "type": "object",
        "properties": {},
        "required": [],
    },
    category="files",
)
def list_cleanable_files() -> dict[str, Any]:
    items = file_ops.list_cleanable()
    return {"results": items, "count": len(items)}


@tool(
    "clean_files",
    "Delete cleanable temporary/cache files by their ids. This is a destructive action "
    "and requires user confirmation.",
    {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of cleanable item ids (from list_cleanable_files).",
            },
        },
        "required": ["items"],
    },
    require_confirmation=True,
    category="files",
)
def clean_files(items: list[str], confirmation_id: str | None = None) -> dict[str, Any]:
    result = file_ops.clean_items(items)
    result.setdefault("ok", True)
    return result

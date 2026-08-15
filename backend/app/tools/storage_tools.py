"""Function-Calling tools for storage analysis and cleanup."""
from __future__ import annotations

from typing import Any

from app import db
from app.services import storage_analysis
from app.tools.registry import tool


@tool(
    name="generate_storage_report",
    description="生成存储分析报告：磁盘使用、大文件 Top10、重复文件统计与清理建议。",
    parameters={"type": "object", "properties": {}, "required": []},
    category="storage",
)
def generate_storage_report() -> dict[str, Any]:
    return storage_analysis.generate_report()


@tool(
    name="list_storage_reports",
    description="列出历史存储分析报告（最新的在前）。",
    parameters={"type": "object", "properties": {}, "required": []},
    category="storage",
)
def list_storage_reports() -> list[dict[str, Any]]:
    return storage_analysis.list_reports()


@tool(
    name="list_cleanable_files",
    description="列出可清理的临时/缓存文件及其大小。",
    parameters={"type": "object", "properties": {}, "required": []},
    category="storage",
)
def list_cleanable_files() -> list[dict[str, Any]]:
    return storage_analysis.list_cleanable()


@tool(
    name="clean_files",
    description="删除指定的可清理文件（危险操作，执行前需要用户确认）。",
    parameters={
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要清理的文件或可清理项 ID 列表",
            }
        },
        "required": ["items"],
    },
    require_confirmation=True,
    category="storage",
)
def clean_files(items: list[str], **kwargs: Any) -> dict[str, Any]:
    result = storage_analysis.clean_items(items)
    db.log_operation("tool_clean_files", {"items": items}, result)
    return result

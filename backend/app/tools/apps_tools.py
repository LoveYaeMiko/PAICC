"""Function-calling tools for application management."""
from __future__ import annotations

from typing import Any

from app.services import app_manager
from app.tools.registry import tool


@tool(
    name="list_applications",
    description="List applications indexed on this machine, optionally only the user's favorites.",
    parameters={
        "type": "object",
        "properties": {
            "favorites_only": {
                "type": "boolean",
                "description": "Only return applications marked as favorites.",
            },
        },
        "required": [],
    },
    category="apps",
)
def list_applications(favorites_only: bool = False) -> dict[str, Any]:
    apps = app_manager.list_apps(favorites_only)
    return {"ok": True, "applications": apps, "count": len(apps)}


@tool(
    name="launch_application",
    description="Launch an application by its id (as returned by list_applications).",
    parameters={
        "type": "object",
        "properties": {
            "id": {
                "type": "integer",
                "description": "Application id from list_applications.",
            },
        },
        "required": ["id"],
    },
    category="apps",
)
def launch_application(id: int) -> dict[str, Any]:
    return app_manager.start_app(id)


@tool(
    name="uninstall_application",
    description="Uninstall an application by its id by running its registry UninstallString.",
    parameters={
        "type": "object",
        "properties": {
            "id": {
                "type": "integer",
                "description": "Application id from list_applications.",
            },
        },
        "required": ["id"],
    },
    require_confirmation=True,
    category="apps",
)
def uninstall_application(id: int, confirmation_id: str | None = None) -> dict[str, Any]:
    return app_manager.uninstall_app(id, confirmation_id)

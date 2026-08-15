"""Dangerous-operation confirmation gate.

A *pending* confirmation is created with :meth:`ConfirmationManager.create`; it must be
approved (or denied) by the user within ``timeout`` seconds before the guarded operation
is allowed to proceed. This is the single source of truth for the "modify (confirm)"
permission level in the API design.
"""
from __future__ import annotations

import secrets
import threading
import time
from typing import Any


class ConfirmationManager:
    def __init__(self, timeout: int = 30) -> None:
        self.timeout = timeout
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def create(self, action: str, title: str, details: Any = None) -> dict[str, Any]:
        cid = secrets.token_hex(8)
        now = time.time()
        item = {
            "confirmation_id": cid,
            "action": action,
            "title": title,
            "details": details or {},
            "status": "pending",
            "created_at": now,
            "expires_at": now + self.timeout,
        }
        with self._lock:
            self._cleanup_locked(now)
            self._items[cid] = item
        return item

    def approve(self, cid: str) -> bool:
        return self._set_status(cid, "approved")

    def deny(self, cid: str) -> bool:
        return self._set_status(cid, "denied")

    def _set_status(self, cid: str, status: str) -> bool:
        with self._lock:
            item = self._items.get(cid)
            if item is None or item["status"] != "pending":
                return False
            if time.time() > item["expires_at"]:
                item["status"] = "expired"
                return False
            item["status"] = status
            return True

    def is_approved(self, cid: str) -> bool:
        with self._lock:
            item = self._items.get(cid)
            if item is None or item["status"] != "approved":
                return False
            return time.time() <= item["expires_at"]

    def get(self, cid: str) -> dict[str, Any] | None:
        with self._lock:
            return self._items.get(cid)

    def list_pending(self) -> list[dict[str, Any]]:
        with self._lock:
            self._cleanup_locked(time.time())
            return [i for i in self._items.values() if i["status"] == "pending"]

    def _cleanup_locked(self, now: float) -> None:
        expired = [cid for cid, i in self._items.items() if i["status"] == "pending" and now > i["expires_at"]]
        for cid in expired:
            self._items[cid]["status"] = "expired"


confirmations = ConfirmationManager()

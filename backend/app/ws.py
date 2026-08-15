"""WebSocket event bus.

Services publish typed events with :func:`publish`; every connected client receives
them as ``{"event": <name>, "data": <payload>}``. ``publish`` is thread-safe — it can
be called from asyncio tasks, watchdog callbacks and APScheduler threads alike.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self) -> None:
        self.active: set[WebSocket] = set()
        self.queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self.queue = asyncio.Queue()
            self._task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        assert self.queue is not None
        while True:
            event, payload = await self.queue.get()
            dead: list[WebSocket] = []
            for ws in list(self.active):
                try:
                    await ws.send_json({"event": event, "data": payload})
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.active.discard(ws)

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self.active.discard(ws)

    def push(self, event: str, payload: Any) -> None:
        if self.queue is not None:
            try:
                self.queue.put_nowait((event, payload))
            except Exception:
                logger.exception("failed to enqueue event %s", event)


manager = ConnectionManager()


def publish(event: str, payload: Any) -> None:
    """Publish an event to all connected clients (thread-safe)."""
    manager.push(event, payload)

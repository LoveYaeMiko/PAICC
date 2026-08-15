"""In-memory asynchronous task registry.

Long-running operations (large-file scans, duplicate detection) are launched here
so the HTTP layer can return a ``task_id`` immediately. Each task runs on its own
``threading.Thread`` and its state is pushed to the frontend via ``ws.publish``
under the ``task_progress`` event.
"""
from __future__ import annotations

import threading
import uuid
from typing import Any, Callable

from app import ws

#: Registry of all tasks keyed by their id.
_tasks: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()
#: Thread-local storage so a running task can update itself without holding an id.
_current = threading.local()


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def start_task(name: str, fn: Callable[..., Any], *args: Any) -> str:
    """Run ``fn(*args)`` on a background thread and return its task id.

    The task is tracked as ``{id, name, status, progress, result, error}`` and every
    state change is published on the ``task_progress`` event.
    """
    task_id = _new_id()
    task: dict[str, Any] = {
        "id": task_id,
        "name": name,
        "status": "running",
        "progress": 0.0,
        "result": None,
        "error": None,
    }
    with _lock:
        _tasks[task_id] = task
    ws.publish("task_progress", dict(task))

    thread = threading.Thread(
        target=_run, args=(task_id, fn, args), name=f"task-{name}", daemon=True
    )
    thread.start()
    return task_id


def _run(task_id: str, fn: Callable[..., Any], args: tuple[Any, ...]) -> None:
    _current.task_id = task_id
    try:
        result = fn(*args)
        with _lock:
            task = _tasks.get(task_id)
            if task is not None:
                task["status"] = "done"
                task["progress"] = 1.0
                task["result"] = result
                payload = dict(task)
        ws.publish("task_progress", payload)
    except Exception as exc:  # noqa: BLE001
        with _lock:
            task = _tasks.get(task_id)
            if task is not None:
                task["status"] = "error"
                task["error"] = f"{type(exc).__name__}: {exc}"
                payload = dict(task)
        ws.publish("task_progress", payload)
    finally:
        _current.task_id = None


def update_task(
    task_id: str | None = None,
    *,
    progress: float | None = None,
    result: Any = None,
) -> None:
    """Update a task's progress/result and publish the change.

    ``task_id`` may be omitted when called from inside the task's own thread (the id
    is resolved from thread-local storage). Safe to call when no task is active — it
    simply does nothing.
    """
    if task_id is None:
        task_id = getattr(_current, "task_id", None)
    if task_id is None:
        return
    with _lock:
        task = _tasks.get(task_id)
        if task is None:
            return
        if progress is not None:
            task["progress"] = max(0.0, min(1.0, float(progress)))
        if result is not None:
            task["result"] = result
        payload = dict(task)
    ws.publish("task_progress", payload)


def get_task(task_id: str) -> dict[str, Any] | None:
    """Return a copy of the task state, or ``None`` if unknown."""
    with _lock:
        task = _tasks.get(task_id)
        return dict(task) if task is not None else None

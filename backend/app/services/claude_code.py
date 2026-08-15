"""Claude Code CLI integration.

Wraps the local ``claude`` CLI (``--output-format stream-json``) as a long-running
subprocess. A daemon reader thread parses each JSON line from stdout and publishes it
as a ``claude_event`` WebSocket event. stderr is drained by a second daemon thread so
the child process never blocks on a full pipe.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

from app import db
from app.config import settings
from app.ws import publish

logger = logging.getLogger(__name__)

#: The running ``claude`` subprocess (``None`` when not running).
proc: subprocess.Popen[str] | None = None
#: Whether the ``claude`` CLI is resolvable on PATH.
available: bool = bool(shutil.which("claude"))
#: Whether a session is currently running.
running: bool = False


def _resolve_claude() -> str | None:
    """Resolve the ``claude`` CLI path from config, falling back to PATH lookup."""
    configured = str(settings.get("claude_path", "") or "").strip()
    if configured and (os.path.isfile(configured) or shutil.which(configured)):
        return configured
    return shutil.which("claude")


def _claude_command() -> list[str]:
    """Build the spawn argv, routing ``.cmd``/``.bat`` through ``cmd /c`` on Windows."""
    path = _resolve_claude() or "claude"
    args = [path, "--output-format", "stream-json"]
    if os.name == "nt" and path.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", *args]
    return args


#: Guards access to :data:`proc` / :data:`running`.
_lock = threading.Lock()
#: Boot-time guard so :func:`start` is idempotent.
_started = False

#: Normalised event types exposed to the frontend.
_TYPE_MAP: dict[str, str] = {
    "system": "system",
    "init": "system",
    "assistant": "assistant",
    "tool_use": "tool_use",
    "tool_result": "tool_result",
    "result": "result",
    "error": "error",
}


def _normalize_type(raw: str) -> str:
    """Map a raw CLI event type to a stable frontend type."""
    return _TYPE_MAP.get(raw, raw or "unknown")


def _cwd() -> str:
    """Pick the working directory for the CLI: the quant project if present, else home."""
    quant_root = settings.get_quant_root()
    if quant_root and Path(quant_root).exists():
        return quant_root
    return str(Path.home())


def _publish_line(line: str) -> None:
    """Parse one line of CLI output and publish it as a typed event."""
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        publish("claude_event", {"type": "raw", "raw": line})
        return
    if isinstance(data, dict):
        raw_type = str(data.get("type", ""))
        event = dict(data)
        event["type"] = _normalize_type(raw_type)
        event["raw_type"] = raw_type
        publish("claude_event", event)
    else:
        publish("claude_event", {"type": "raw", "raw": line})


def _mark_ended(p: subprocess.Popen[str]) -> None:
    """Clear module state when the given (now-exited) process was the active one."""
    global proc, running
    with _lock:
        if proc is p:
            proc = None
            running = False


def _stdout_reader(p: subprocess.Popen[str]) -> None:
    assert p.stdout is not None
    try:
        for line in p.stdout:
            line = line.strip()
            if line:
                _publish_line(line)
    except (OSError, ValueError):
        pass
    finally:
        _mark_ended(p)
        publish("claude_event", {"type": "system", "subtype": "exit", "message": "session ended"})


def _stderr_reader(p: subprocess.Popen[str]) -> None:
    assert p.stderr is not None
    try:
        for line in p.stderr:
            line = line.strip()
            if line:
                publish("claude_event", {"type": "error", "source": "stderr", "message": line})
    except (OSError, ValueError):
        pass


def start() -> None:
    """Idempotent boot hook — only records CLI availability, never auto-starts a session."""
    global _started, available
    if _started:
        return
    available = _resolve_claude() is not None
    _started = True


def start_session() -> dict[str, Any]:
    """Launch a ``claude`` CLI session and begin streaming its output."""
    global proc, running
    if not available:
        return {"ok": False, "error": "claude CLI not found in PATH"}

    with _lock:
        if running and proc is not None and proc.poll() is None:
            return {"ok": True, "pid": proc.pid}

        cwd = _cwd()
        try:
            proc = subprocess.Popen(
                _claude_command(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=cwd,
                bufsize=1,
            )
        except OSError as exc:
            proc = None
            running = False
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        running = True
        pid = proc.pid
        threading.Thread(target=_stdout_reader, args=(proc,), daemon=True, name="claude-stdout").start()
        threading.Thread(target=_stderr_reader, args=(proc,), daemon=True, name="claude-stderr").start()

    db.log_operation("claude_session_start", {"cwd": cwd}, {"pid": pid})
    return {"ok": True, "pid": pid}


def send_message(text: str) -> dict[str, Any]:
    """Send a line of input to the running session (auto-starting if needed)."""
    if not running or proc is None or proc.poll() is not None:
        res = start_session()
        if not res.get("ok"):
            return res

    with _lock:
        if proc is None or proc.stdin is None or proc.poll() is not None:
            return {"ok": False, "error": "claude session is not running"}
        try:
            proc.stdin.write(text + "\n")
            proc.stdin.flush()
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    publish("claude_event", {"type": "user", "message": text})
    db.log_operation("claude_message_sent", {"text": text}, {"ok": True})
    return {"ok": True}


def stop_session() -> dict[str, Any]:
    """Terminate the running session, if any."""
    global proc, running
    with _lock:
        p = proc
        proc = None
        running = False
        if p is not None and p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass
    db.log_operation("claude_session_stop", {}, {"ok": True})
    return {"ok": True}


def status() -> dict[str, Any]:
    """Return CLI availability, running state, and the active pid (if any)."""
    pid: int | None = None
    with _lock:
        if proc is not None and proc.poll() is None:
            pid = proc.pid
    return {"available": available, "running": running, "pid": pid}

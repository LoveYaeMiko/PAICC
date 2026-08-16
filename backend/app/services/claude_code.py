"""Claude Code CLI integration.

Wraps the local ``claude`` CLI (``--output-format stream-json``) as a long-running
subprocess. A daemon reader thread parses each JSON line from stdout and publishes it
as a ``claude_event`` WebSocket event. stderr is drained by a second daemon thread so
the child process never blocks on a full pipe.
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
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


# --------------------------------------------------------------------------- #
# File-change watcher (polling snapshot, no third-party dependencies)
# --------------------------------------------------------------------------- #
#: Directory names skipped while walking the watched tree.
_FILE_IGNORE_DIRS = {
    ".git", ".svn", ".hg", ".claude", ".idea", ".vscode", ".pytest_cache",
    ".venv", "venv", "env", "node_modules", "__pycache__", "build", "dist",
    "logs",
}
#: Files larger than this (bytes) are not snapshotted into memory for diffs.
_MAX_SNAPSHOT_BYTES = 512 * 1024
#: Maximum number of diff lines emitted in a single ``file_changed`` event.
_MAX_DIFF_LINES = 800
#: Polling interval (seconds) between directory scans.
_WATCH_INTERVAL = 2.0

#: Watcher state — owned exclusively by the single polling thread.
_watch_started = False
_watch_dir: str | None = None
_watch_snapshot: dict[str, tuple[int, int]] = {}
_watch_content: dict[str, list[str]] = {}
_git_top_cache: dict[str, str | None] = {}


def _resolve_claude() -> str | None:
    """Resolve the ``claude`` CLI path from config, falling back to PATH lookup."""
    configured = str(settings.get("claude_path", "") or "").strip()
    if configured and (os.path.isfile(configured) or shutil.which(configured)):
        return configured
    return shutil.which("claude")


def _build_context() -> str:
    """Build a short single-line system-state summary to inject into the session.

    Lazily imports ``system_monitor`` / ``quant_manager`` and tolerates any failure
    so context injection can never block or break session startup. The result is
    deliberately one line: a multi-line argument is truncated by ``cmd /c`` on
    Windows when the CLI is launched through its ``claude.cmd`` shim.
    """
    parts = ["[PAICC system context]"]
    try:
        from app.services import system_monitor

        stats = system_monitor.get_system_stats()
        cpu = stats.get("cpu_percent")
        mem = (stats.get("memory") or {}).get("percent")
        if cpu is not None and mem is not None:
            parts.append(f"CPU {cpu}%, memory {mem}%")
    except Exception as exc:  # noqa: BLE001
        logger.debug("system stats unavailable for context: %s", exc)

    try:
        from app.services import quant_manager

        status = quant_manager.get_status()
        overall = str(status.get("overall") or "unknown")
        red_lines = status.get("red_lines") or []
        # Use the ASCII ``name`` (not the localised ``label``) so the injected
        # context stays locale-independent and safe to pass through ``cmd /c``.
        summary = "; ".join(
            f"{rl.get('name') or rl.get('label')}={rl.get('level')}"
            for rl in red_lines
        )
        parts.append(f"quant red lines[{overall}]: {summary or 'none'}")
    except Exception as exc:  # noqa: BLE001
        logger.debug("quant status unavailable for context: %s", exc)

    parts.append(f"cwd={_cwd()}")
    context = " ; ".join(parts)
    return context.replace("\r", " ").replace("\n", " ")


def _cmd_escape_arg(value: str) -> str:
    """Escape one argument so ``cmd /c`` re-parses it literally.

    ``cmd.exe`` re-parses the joined command line, so an argument containing spaces
    or metacharacters must be wrapped in double quotes, ``%`` must be doubled to
    suppress environment-variable expansion, and embedded double quotes are doubled
    (``""``) so they survive as literal quotes for the child shell.
    """
    value = str(value).replace("%", "%%").replace('"', '""')
    if not value:
        return '""'
    if re.search(r'[\s&|<>^()"]', value):
        return f'"{value}"'
    return value


def _claude_command() -> list[str]:
    """Build the spawn argv, routing ``.cmd``/``.bat`` through ``cmd /c`` on Windows.

    The ``--append-system-prompt`` flag (verified against the installed CLI) injects
    a one-line system-state summary as extra context for the session. When routing
    through ``cmd``, each argument is escaped and the command is passed as a single
    string so ``cmd`` re-parses it correctly (avoids metacharacter injection and
    broken multi-word flags).
    """
    path = _resolve_claude() or "claude"
    args = [path, "--output-format", "stream-json"]
    context = _build_context()
    if context:
        args += ["--append-system-prompt", context]
    if os.name == "nt" and path.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", " ".join(_cmd_escape_arg(a) for a in args)]
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


# --------------------------------------------------------------------------- #
# File-change watcher implementation
# --------------------------------------------------------------------------- #
def _read_text_lines(path: str) -> list[str] | None:
    """Read a file as text lines for diffing, or ``None`` if too large/unreadable."""
    try:
        if os.path.getsize(path) > _MAX_SNAPSHOT_BYTES:
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines(keepends=True)
    except OSError:
        return None


def _git_top_level(root: str) -> str | None:
    """Return the git work-tree top containing ``root`` (cached), else ``None``."""
    key = os.path.normcase(os.path.abspath(root))
    if key in _git_top_cache:
        return _git_top_cache[key]
    top: str | None = None
    try:
        proc = subprocess.run(
            ["git", "-C", root, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0:
            candidate = proc.stdout.strip()
            if candidate:
                top = candidate
    except (OSError, subprocess.TimeoutExpired):
        top = None
    _git_top_cache[key] = top
    return top


def _truncate_diff(text: str) -> str:
    """Cap a diff to ``_MAX_DIFF_LINES`` lines to keep events bounded."""
    lines = text.splitlines()
    if len(lines) > _MAX_DIFF_LINES:
        head = "\n".join(lines[:_MAX_DIFF_LINES])
        return f"{head}\n... ({len(lines) - _MAX_DIFF_LINES} more diff lines)"
    return text


def _added_file_diff(rel: str, full: str) -> str:
    """Render a new file as a whole-file addition (``+``-prefixed lines)."""
    lines = _read_text_lines(full)
    if lines is None:
        return f"new file: {rel}\n(binary or too large to display)"
    truncated = len(lines) > _MAX_DIFF_LINES
    shown = lines[:_MAX_DIFF_LINES] if truncated else lines
    body = "".join(f"+{ln}" for ln in shown)
    if body and not body.endswith("\n"):
        body += "\n"
    note = f"... ({len(lines) - _MAX_DIFF_LINES} more lines)" if truncated else ""
    return f"new file: {rel}\n{body}{note}"


def _git_diff(top: str, rel: str, full: str, kind: str) -> str | None:
    """Compute a diff via git; new files are rendered as whole-file additions."""
    if kind == "added":
        return _added_file_diff(rel, full)
    rel_top = os.path.relpath(full, top).replace(os.sep, "/")
    try:
        proc = subprocess.run(
            ["git", "-C", top, "diff", "--", rel_top],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout
    if not out.strip():
        return None
    return _truncate_diff(out)


def _fallback_diff(root: str, rel: str, kind: str) -> str | None:
    """Diff against the cached in-memory snapshot (difflib) when git is absent."""
    full = os.path.join(root, rel)
    if kind == "added":
        return _added_file_diff(rel, full)
    new_lines = _read_text_lines(full)
    old_lines = _watch_content.get(rel)
    _watch_content[rel] = new_lines if new_lines is not None else []
    if new_lines is None:
        return f"modified: {rel}\n(binary or too large to diff)"
    if old_lines is None:
        return _added_file_diff(rel, full)
    text = "".join(
        difflib.unified_diff(
            old_lines, new_lines, fromfile=f"a/{rel}", tofile=f"b/{rel}"
        )
    )
    return _truncate_diff(text)


def _emit_file_change(root: str, rel: str, kind: str) -> None:
    """Publish a ``claude_event`` of type ``file_changed`` for one path."""
    full = os.path.join(root, rel)
    if kind == "deleted":
        diff = f"deleted: {rel}"
        _watch_content.pop(rel, None)
    elif _git_top_level(root) is not None:
        diff = _git_diff(_git_top_level(root), rel, full, kind)
    else:
        diff = _fallback_diff(root, rel, kind)
    if not diff or not diff.strip():
        return
    publish(
        "claude_event",
        {"type": "file_changed", "file": rel, "diff": diff, "timestamp": time.time()},
    )


def _build_snapshot(root: str) -> dict[str, tuple[int, int]]:
    """Walk the tree once and return ``{relpath: (mtime_ns, size)}``."""
    snapshot: dict[str, tuple[int, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in _FILE_IGNORE_DIRS and not d.startswith(".")
        ]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            try:
                st = os.stat(full)
            except OSError:
                continue
            snapshot[os.path.relpath(full, root)] = (st.st_mtime_ns, st.st_size)
    return snapshot


def _file_watch_loop() -> None:
    """Poll the working directory and publish ``file_changed`` events on changes."""
    global _watch_dir, _watch_snapshot, _watch_content
    while True:
        try:
            root = _cwd()
            if root and Path(root).is_dir():
                if root != _watch_dir:
                    _watch_dir = root
                    _watch_snapshot = {}
                    _watch_content = {}
                    first = True
                else:
                    first = not _watch_snapshot

                current = _build_snapshot(root)

                # Seed the in-memory content snapshot once so the difflib fallback
                # (no-git case) has a baseline to diff against.
                if first and _git_top_level(root) is None:
                    for rel in current:
                        lines = _read_text_lines(os.path.join(root, rel))
                        _watch_content[rel] = lines if lines is not None else []

                for rel, stat in current.items():
                    prev = _watch_snapshot.get(rel)
                    if prev is None:
                        if not first:
                            _emit_file_change(root, rel, "added")
                    elif prev != stat:
                        _emit_file_change(root, rel, "modified")

                for rel in list(_watch_snapshot.keys()):
                    if rel not in current:
                        _emit_file_change(root, rel, "deleted")

                _watch_snapshot = current
        except Exception:  # noqa: BLE001
            logger.exception("claude file watcher tick failed")
        time.sleep(_WATCH_INTERVAL)


def _start_file_watcher() -> None:
    """Start the polling watcher thread (idempotent)."""
    global _watch_started
    if _watch_started:
        return
    _watch_started = True
    threading.Thread(target=_file_watch_loop, daemon=True, name="claude-file-watch").start()
    logger.info("claude file watcher started on %s", _cwd())


def start() -> None:
    """Idempotent boot hook — only records CLI availability, never auto-starts a session."""
    global _started, available
    if _started:
        return
    available = _resolve_claude() is not None
    _started = True
    _start_file_watcher()


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

"""Quant project monitoring service.

Wraps the external quant project (default ``C:\\Users\\wyxwi\\Desktop\\FQA``):

* register / detect projects and their key files (configs, CLI entry, scripts);
* read the four canonical "red line" health indicators (成本偏离 / 空腿偏差 /
  Regime 切换 / PEAD 异常) from a dashboard script, a dashboard JSON file, or the
  project config as a last resort;
* monitor matching OS processes (via psutil) and tail the newest log file;
* manage preset commands and run them (subprocess) after confirmation;
* run an idempotent background watchdog (tail the log dir) + a 10s status poller
  that pushes ``log_line`` / ``red_line_alert`` events over the WebSocket bus.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import psutil

from app import db
from app.config import settings
from app.ws import publish

logger = logging.getLogger(__name__)

#: Canonical red-line definitions. ``kind="deviation"`` lines are percentage
#: deviations mapped through warning/critical thresholds; ``kind="flag"`` lines are
#: boolean-style anomalies (a truthy value means critical).
_RED_LINE_SPECS: list[dict[str, Any]] = [
    {"name": "cost_deviation", "label": "成本偏离", "kind": "deviation", "threshold": 10.0, "critical": 20.0},
    {"name": "short_leg_deviation", "label": "空腿偏差", "kind": "deviation", "threshold": 10.0, "critical": 20.0},
    {"name": "regime_switch", "label": "Regime 切换", "kind": "flag", "threshold": None, "critical": None},
    {"name": "pead_anomaly", "label": "PEAD 异常", "kind": "flag", "threshold": None, "critical": None},
]

_ERROR_MARKERS = ("error", "trace", "exception", "错误")

_started = False
_start_lock = threading.Lock()
_observer: Any = None
_last_overall: str | None = None
_last_criticals: set[str] = set()


# --------------------------------------------------------------------------- #
# Projects
# --------------------------------------------------------------------------- #
def list_projects() -> list[dict[str, Any]]:
    """Return all registered quant projects (active first)."""
    return db.query("SELECT * FROM quant_projects ORDER BY is_active DESC, id ASC")


def register_project(
    name: str,
    root_path: str,
    config_file: str | None = None,
    log_dir: str | None = None,
) -> dict[str, Any]:
    """Upsert a project keyed by ``root_path`` and return the resulting row."""
    root_path = str(root_path)
    existing = db.query_one("SELECT * FROM quant_projects WHERE root_path = ?", (root_path,))
    if existing:
        db.execute(
            "UPDATE quant_projects SET name = ?, config_file = ?, log_dir = ? WHERE id = ?",
            (
                name,
                config_file if config_file is not None else existing["config_file"],
                log_dir if log_dir is not None else existing["log_dir"],
                existing["id"],
            ),
        )
        db.log_operation("quant_register_project", {"name": name, "root_path": root_path}, {"updated": True})
        return db.query_one("SELECT * FROM quant_projects WHERE id = ?", (existing["id"],))  # type: ignore[return-value]

    cid = db.execute(
        "INSERT INTO quant_projects(name, root_path, config_file, log_dir, is_active) "
        "VALUES (?, ?, ?, ?, 1)",
        (name, root_path, config_file or "", log_dir or "logs"),
    )
    db.log_operation("quant_register_project", {"name": name, "root_path": root_path}, {"id": cid})
    return db.query_one("SELECT * FROM quant_projects WHERE id = ?", (cid,))  # type: ignore[return-value]


def detect_project(root_path: str) -> dict[str, Any]:
    """Scan ``root_path`` for the quant project's key files and return a report."""
    root = Path(root_path)
    config_files: list[str] = []
    entry_points: list[str] = []

    configs_dir = root / "configs"
    if configs_dir.is_dir():
        for pattern in ("*.yaml", "*.yml"):
            config_files.extend(
                p.relative_to(root).as_posix() for p in sorted(configs_dir.glob(pattern))
            )

    master = root / "master_config.yaml"
    if master.is_file():
        master_rel = "master_config.yaml"
        if master_rel not in config_files:
            config_files.append(master_rel)

    for candidate in ("src/cli.py", "cli.py", "main.py"):
        if (root / candidate).is_file():
            entry_points.append(candidate)

    scripts_dir = root / "scripts"
    if scripts_dir.is_dir():
        entry_points.extend(
            p.relative_to(root).as_posix() for p in sorted(scripts_dir.glob("*.py"))
        )

    log_dir: str | None = None
    if (root / "logs").is_dir():
        log_dir = str(root / "logs")

    has_simulation_dashboard = bool(
        (root / "outputs" / "phase10_dashboard.json").is_file()
        or (root / "data" / "red_lines.json").is_file()
        or (root / "scripts" / "phase10_dashboard.py").is_file()
        or any("dashboard" in f.lower() for f in config_files)
    )

    return {
        "root_path": str(root),
        "config_files": config_files,
        "entry_points": entry_points,
        "log_dir": log_dir,
        "has_simulation_dashboard": has_simulation_dashboard,
    }


# --------------------------------------------------------------------------- #
# Red-line status
# --------------------------------------------------------------------------- #
def get_status() -> dict[str, Any]:
    """Return the four canonical red lines plus an aggregate ``overall`` severity.

    Resolution order:

    1. if ``quant_dashboard_script`` is configured, run ``python <script> --json``
       with cwd = project root (10s timeout) and parse the JSON;
    2. else parse ``outputs/phase10_dashboard.json`` or ``data/red_lines.json``;
    3. else parse the project config YAML for threshold keys and report every line
       with ``level="unknown"`` and detail ``"dashboard not found"``.
    """
    timestamp = time.time()
    root = Path(_project_root())

    # (a) dashboard script
    script = str(settings.get("quant_dashboard_script", "") or "").strip()
    if script:
        data = _run_dashboard_script(root, script)
        if data is not None:
            return _build_status(data, source=f"script:{script}", timestamp=timestamp)

    # (b) dashboard JSON files
    for rel in ("outputs/phase10_dashboard.json", "data/red_lines.json"):
        path = root / rel
        if path.is_file():
            data = _parse_json_file(path)
            if data is not None:
                return _build_status(data, source=rel, timestamp=timestamp)

    # (c) config thresholds, unknown level
    thresholds = _load_thresholds_from_config(root)
    red_lines: list[dict[str, Any]] = []
    for spec in _RED_LINE_SPECS:
        threshold = spec.get("threshold")
        critical = spec.get("critical")
        cfg = thresholds.get(spec["name"])
        if cfg:
            threshold = cfg.get("threshold", threshold)
            critical = cfg.get("critical", critical)
        red_lines.append(
            {
                "name": spec["name"],
                "label": spec["label"],
                "level": "unknown",
                "value": None,
                "threshold": threshold,
                "critical": critical,
                "detail": "dashboard not found",
            }
        )
    return {
        "timestamp": timestamp,
        "overall": _overall([rl["level"] for rl in red_lines]),
        "red_lines": red_lines,
        "source": "config",
    }


def _run_dashboard_script(root: Path, script: str) -> Any | None:
    try:
        proc = subprocess.run(
            ["python", script, "--json"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("quant dashboard script failed: %s", exc)
        return None
    out = (proc.stdout or "") or (proc.stderr or "")
    return _parse_json(out)


def _parse_json(text: str) -> Any | None:
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except Exception:  # noqa: BLE001
            return None
    return None


def _parse_json_file(path: Path) -> Any | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to read dashboard JSON %s: %s", path, exc)
        return None
    return _parse_json(text)


def _build_status(data: Any, source: str, timestamp: float) -> dict[str, Any] | None:
    if isinstance(data, list):
        payload: Any = {"red_lines": data}
    elif isinstance(data, dict):
        payload = data
    else:
        return None

    red_lines = [_extract_line(payload, spec) for spec in _RED_LINE_SPECS]
    return {
        "timestamp": timestamp,
        "overall": _overall([rl["level"] for rl in red_lines]),
        "red_lines": red_lines,
        "source": source,
    }


def _extract_line(payload: Any, spec: dict[str, Any]) -> dict[str, Any]:
    name = spec["name"]
    raw = _locate_entry(payload, name)

    value: Any = None
    level: Any = None
    threshold = spec.get("threshold")
    critical = spec.get("critical")
    detail = ""

    if isinstance(raw, dict):
        value = raw.get("value", raw.get("val", raw.get("current")))
        if value is None:
            value = raw.get("deviation", raw.get("pct"))
        level = raw.get("level", raw.get("status"))
        threshold = raw.get("threshold", raw.get("warning", threshold))
        critical = raw.get("critical", critical)
        detail = str(raw.get("detail", raw.get("message", "")))
    else:
        value = raw

    if level is None:
        level = _level_for(spec, value)

    return {
        "name": name,
        "label": spec["label"],
        "level": _normalize_level(level),
        "value": value,
        "threshold": threshold,
        "critical": critical,
        "detail": detail,
    }


def _locate_entry(payload: Any, name: str) -> Any:
    """Locate a red-line value inside a dashboard dict (list entry, status map, or deep key)."""
    if isinstance(payload, dict):
        red_lines = payload.get("red_lines")
        if isinstance(red_lines, list):
            for entry in red_lines:
                if isinstance(entry, dict) and _match_name(entry.get("name"), name):
                    return entry
        status = payload.get("status")
        if isinstance(status, dict):
            nested = status.get("red_lines")
            if isinstance(nested, list):
                for entry in nested:
                    if isinstance(entry, dict) and _match_name(entry.get("name"), name):
                        return entry
            for key, val in status.items():
                if _match_name(key, name):
                    return val
    return _find_key(payload, name)


def _find_key(data: Any, name: str) -> Any:
    if isinstance(data, dict):
        for key, val in data.items():
            if _match_name(key, name):
                return val
            found = _find_key(val, name)
            if found is not None:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _find_key(item, name)
            if found is not None:
                return found
    return None


def _match_name(a: Any, b: Any) -> bool:
    return _normalize(a) == _normalize(b)


def _normalize(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.lower().replace("_", "").replace(" ", "").replace("-", "")


def _level_for(spec: dict[str, Any], value: Any) -> str:
    if value is None or value == "":
        return "unknown"
    if spec["kind"] == "flag":
        return "critical" if _truthy(value) else "ok"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "unknown"
    critical = spec.get("critical")
    threshold = spec.get("threshold")
    if critical is not None and num >= float(critical):
        return "critical"
    if threshold is not None and num >= float(threshold):
        return "warning"
    return "ok"


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y", "active", "triggered", "anomaly", "switch")


def _normalize_level(level: Any) -> str:
    if level is None:
        return "unknown"
    s = str(level).strip().lower()
    if s in ("critical", "red", "error", "high", "severe", "fatal"):
        return "critical"
    if s in ("warning", "warn", "yellow", "medium", "caution"):
        return "warning"
    if s in ("ok", "normal", "green", "low", "none", "pass", "0", "false", "clear"):
        return "ok"
    return "unknown"


def _overall(levels: list[str]) -> str:
    if any(level == "critical" for level in levels):
        return "critical"
    if any(level in ("warning", "unknown") for level in levels):
        return "warning"
    return "ok"


def _load_thresholds_from_config(root: Path) -> dict[str, dict[str, Any]]:
    config_dir = root / "configs"
    candidates = [
        config_dir / "master_config.yaml",
        root / "master_config.yaml",
        config_dir / "factor_thresholds.yaml",
    ]
    merged: dict[str, Any] = {}
    for path in candidates:
        if not path.is_file():
            continue
        data = _load_yaml(path)
        if isinstance(data, dict):
            merged.update(data)

    result: dict[str, dict[str, Any]] = {}
    for spec in _RED_LINE_SPECS:
        if spec["kind"] != "deviation":
            continue
        name = spec["name"]
        entry: dict[str, Any] = {}
        for key in (f"{name}_threshold", f"{name}_warning", f"{name}_critical"):
            val = _find_key(merged, key)
            if _is_number(val):
                if key.endswith("_critical"):
                    entry["critical"] = float(val)
                else:
                    entry["threshold"] = float(val)
        if not entry:
            val = _find_key(merged, name)
            if _is_number(val):
                entry["threshold"] = float(val)
        if entry:
            result[name] = entry
    return result


def _is_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    return isinstance(value, (int, float))


def _load_yaml(path: Path) -> Any:
    try:
        import yaml  # core dependency, but imported lazily so a missing wheel degrades
    except ImportError:
        logger.warning("PyYAML not installed; cannot parse %s", path)
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return yaml.safe_load(fh)
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to parse yaml %s: %s", path, exc)
        return None


# --------------------------------------------------------------------------- #
# Processes / logs / commands
# --------------------------------------------------------------------------- #
def monitor_processes() -> list[dict[str, Any]]:
    """List processes whose command line mentions the quant project root path."""
    root = _project_root()
    if not root:
        return []
    root_norm = os.path.normcase(os.path.normpath(root))
    result: list[dict[str, Any]] = []
    try:
        for proc in psutil.process_iter(["pid", "cmdline", "cpu_percent", "memory_percent", "create_time"]):
            try:
                info = proc.info
                cmdline = " ".join(info.get("cmdline") or [])
                if root_norm not in os.path.normcase(cmdline):
                    continue
                create_time = info.get("create_time")
                result.append(
                    {
                        "pid": info.get("pid"),
                        "cmdline": cmdline,
                        "cpu_percent": info.get("cpu_percent") or 0.0,
                        "memory_percent": info.get("memory_percent") or 0.0,
                        "running_time": round(time.time() - create_time, 1) if create_time else 0.0,
                    }
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
    except Exception as exc:  # noqa: BLE001
        logger.warning("process scan failed: %s", exc)
    return result


def list_commands(project_id: int | None = None) -> list[dict[str, Any]]:
    """List preset commands, optionally filtered by project."""
    if project_id is None:
        return db.query("SELECT * FROM quant_commands ORDER BY project_id ASC, id ASC")
    return db.query("SELECT * FROM quant_commands WHERE project_id = ? ORDER BY id ASC", (project_id,))


def add_command(project_id: int, name: str, command: str, description: str = "") -> dict[str, Any]:
    """Insert a preset command and return the new row."""
    cid = db.execute(
        "INSERT INTO quant_commands(project_id, name, command, description) VALUES (?, ?, ?, ?)",
        (project_id, name, command, description or ""),
    )
    db.log_operation(
        "quant_add_command",
        {"project_id": project_id, "name": name, "command": command},
        {"id": cid},
    )
    return db.query_one("SELECT * FROM quant_commands WHERE id = ?", (cid,))  # type: ignore[return-value]


def run_command(
    command_id: int | None = None,
    command: str | None = None,
    confirmation_id: str | None = None,
) -> dict[str, Any]:
    """Resolve a command (by id or literal string) and spawn it in the project root.

    Confirmation gating is performed by the caller (router / tool dispatcher); this
    function simply executes the resolved command.
    """
    cmd, root = _resolve_command(command_id, command)
    try:
        proc = subprocess.Popen(
            cmd,
            shell=True,
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("failed to spawn command")
        raise RuntimeError(f"failed to run command: {exc}") from exc

    publish("log_line", {"file": "command", "line": cmd, "level": "info"})
    db.log_operation(
        "quant_run_command",
        {"command_id": command_id, "command": cmd, "cwd": root},
        {"pid": proc.pid},
    )
    return {"pid": proc.pid, "command": cmd}


def _resolve_command(command_id: int | None, command: str | None) -> tuple[str, str]:
    if command:
        return str(command), _project_root()
    if command_id is None:
        raise ValueError("either command_id or command is required")
    row = db.query_one("SELECT * FROM quant_commands WHERE id = ?", (command_id,))
    if row is None:
        raise ValueError(f"command {command_id} not found")
    root = _project_root()
    project = db.query_one("SELECT root_path FROM quant_projects WHERE id = ?", (row["project_id"],))
    if project and project.get("root_path"):
        root = str(project["root_path"])
    return str(row["command"]), root


def tail_log(lines: int = 100) -> list[str]:
    """Return the last ``lines`` of the newest file in the project log directory."""
    _, log_dir = _project_root_and_log_dir()
    log_path = Path(log_dir)
    if not log_path.is_dir():
        return []
    newest: Path | None = None
    try:
        files = [p for p in log_path.iterdir() if p.is_file()]
        newest = max(files, key=lambda p: p.stat().st_mtime) if files else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to scan log dir %s: %s", log_path, exc)
        return []
    if newest is None:
        return []
    try:
        with open(newest, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.readlines()
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to tail %s: %s", newest, exc)
        return []
    return [line.rstrip("\n") for line in content[-lines:]]


# --------------------------------------------------------------------------- #
# Path helpers
# --------------------------------------------------------------------------- #
def _project_root() -> str:
    return str(settings.get_quant_root() or "")


def _project_root_and_log_dir() -> tuple[str, str]:
    project = db.query_one(
        "SELECT root_path, log_dir FROM quant_projects WHERE is_active = 1 ORDER BY id ASC LIMIT 1"
    )
    if project and project.get("root_path"):
        root = str(project["root_path"])
        log_dir = str(project.get("log_dir") or "logs")
        if not os.path.isabs(log_dir):
            log_dir = str(Path(root) / log_dir)
        return root, log_dir
    root = _project_root()
    return root, str(Path(root) / str(settings.get("quant_log_dir", "logs")))


# --------------------------------------------------------------------------- #
# Background service
# --------------------------------------------------------------------------- #
def start() -> None:
    """Idempotent, non-blocking start of the watchdog + status poller."""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True

    _seed_default_commands()
    _start_watchdog()
    threading.Thread(target=_status_poll_loop, daemon=True, name="quant-status-poll").start()
    logger.info("quant_manager background service started")


def _seed_default_commands() -> None:
    for project in list_projects():
        existing = db.query("SELECT id FROM quant_commands WHERE project_id = ?", (project["id"],))
        if existing:
            continue
        root = Path(project["root_path"])
        has_entry = (root / "src" / "cli.py").is_file() or (root / "cli.py").is_file()
        if not has_entry:
            continue
        db.execute(
            "INSERT INTO quant_commands(project_id, name, command, description) VALUES (?, ?, ?, ?)",
            (project["id"], "CLI 帮助", "python cli.py --help", "量化项目 CLI 帮助"),
        )
        db.execute(
            "INSERT INTO quant_commands(project_id, name, command, description) VALUES (?, ?, ?, ?)",
            (project["id"], "Phase10 回测", "python scripts/phase10_backtest.py --help", "Phase10 回测帮助"),
        )


def _start_watchdog() -> None:
    global _observer
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        logger.warning("watchdog not installed; quant log streaming disabled")
        return

    _, log_dir = _project_root_and_log_dir()
    log_path = Path(log_dir)
    if not log_path.is_dir():
        logger.info("quant log dir not found (%s); watchdog skipped", log_path)
        return

    class _LogEventHandler(FileSystemEventHandler):
        def __init__(self) -> None:
            self._offsets: dict[str, int] = {}
            self._lock = threading.Lock()

        def on_created(self, event: Any) -> None:
            if not event.is_directory:
                with self._lock:
                    self._offsets[event.src_path] = 0

        def on_modified(self, event: Any) -> None:
            if event.is_directory:
                return
            self._read_appended(event.src_path)

        def _read_appended(self, path: str) -> None:
            if not os.path.isfile(path):
                return
            try:
                with self._lock:
                    if path not in self._offsets:
                        self._offsets[path] = os.path.getsize(path)  # skip pre-existing content
                    size = os.path.getsize(path)
                    offset = self._offsets[path]
                    if size < offset:  # truncated / rotated
                        offset = 0
                    if size <= offset:
                        return
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        fh.seek(offset)
                        data = fh.read()
                        self._offsets[path] = fh.tell()
            except Exception:  # noqa: BLE001
                logger.exception("failed to read appended log lines from %s", path)
                return

            for line in data.splitlines():
                level = "error" if _is_error_line(line) else "info"
                publish("log_line", {"file": path, "line": line, "level": level})

    try:
        observer = Observer()
        observer.schedule(_LogEventHandler(), str(log_path), recursive=False)
        observer.start()
        _observer = observer
        logger.info("quant watchdog started on %s", log_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("quant watchdog failed to start: %s", exc)


def _is_error_line(line: str) -> bool:
    lower = line.lower()
    return any(marker in lower for marker in _ERROR_MARKERS)


def _status_poll_loop() -> None:
    global _last_overall, _last_criticals
    while True:
        try:
            status = get_status()
            overall = status.get("overall")
            red_lines = status.get("red_lines", [])

            criticals = {rl["name"] for rl in red_lines if rl.get("level") == "critical"}
            new_criticals = criticals - _last_criticals

            if overall != _last_overall:
                publish(
                    "red_line_alert",
                    {"title": f"红线状态变化: {overall}", "message": _summarize(status)},
                )
            for name in sorted(new_criticals):
                rl = next((r for r in red_lines if r.get("name") == name), None)
                if rl is None:
                    continue
                publish(
                    "red_line_alert",
                    {
                        "title": f"{rl.get('label', name)} 红线触发",
                        "message": rl.get("detail") or f"{name} = {rl.get('value')}",
                    },
                )

            _last_overall = overall
            _last_criticals = criticals
        except Exception:  # noqa: BLE001
            logger.exception("quant status poll failed")
        time.sleep(10)


def _summarize(status: dict[str, Any]) -> str:
    parts = [f"{rl.get('label', rl.get('name'))}: {rl.get('level')}" for rl in status.get("red_lines", [])]
    return "; ".join(parts) or "no red lines"

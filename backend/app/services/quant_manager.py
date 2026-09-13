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
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil

from app import db
from app.config import settings
from app.services import knowledge_base
from app.ws import publish

logger = logging.getLogger(__name__)

#: Canonical red-line definitions. Labels mirror FQA's ``build_shadow_status`` so
#: the fallback (payload without a label) stays in sync with the source of truth.
#: ``kind="deviation"`` lines are percentage deviations mapped through
#: warning/critical thresholds; ``kind="flag"`` lines are advisory anomalies
#: (a truthy value means warning — FQA treats them as warnings, not hard halts).
_RED_LINE_SPECS: list[dict[str, Any]] = [
    {"name": "cost_deviation", "label": "成本模型偏差", "kind": "deviation", "threshold": 10.0, "critical": 20.0},
    {"name": "short_leg_deviation", "label": "多空敞口失衡", "kind": "deviation", "threshold": 10.0, "critical": 20.0},
    {"name": "regime_switch", "label": "趋势切换", "kind": "flag", "threshold": None, "critical": None},
    {"name": "pead_anomaly", "label": "PEAD 覆盖异常", "kind": "flag", "threshold": None, "critical": None},
]

_ERROR_MARKERS = ("error", "trace", "exception", "错误")

_started = False
_start_lock = threading.Lock()
_observer: Any = None
_last_overall: str | None = None
_last_criticals: set[str] = set()
_last_history_signature: dict[str, str] | None = None

#: PIDs spawned by :func:`run_command`, keyed by pid -> {pid, project_id, command, root}.
_tracked_lock = threading.Lock()
_tracked: dict[int, dict[str, Any]] = {}


# --------------------------------------------------------------------------- #
# Projects
# --------------------------------------------------------------------------- #
def list_projects() -> list[dict[str, Any]]:
    """Return all registered quant projects (active first)."""
    return db.query("SELECT * FROM quant_projects ORDER BY is_active DESC, id ASC")


def _prefer_master_config(config_files: list[str]) -> str:
    """Pick the canonical config from a detected list.

    ``master_config.yaml`` is the single source of truth; prefer it over the
    alphabetical first hit (which would otherwise be an arbitrary
    ``configs/*.yaml`` sibling such as ``factor_thresholds.yaml``).
    """
    for path in config_files:
        if Path(path).name == "master_config.yaml":
            return path
    return config_files[0]


def register_project(
    name: str,
    root_path: str,
    config_file: str | None = None,
    dashboard_script: str | None = None,
    log_dir: str | None = None,
) -> dict[str, Any]:
    """Upsert a project keyed by ``root_path`` and return the resulting row.

    Empty ``config_file`` / ``dashboard_script`` / ``log_dir`` fields are
    auto-filled by scanning ``root_path`` via :func:`detect_project`.
    """
    root_path = str(root_path)
    existing = db.query_one("SELECT * FROM quant_projects WHERE root_path = ?", (root_path,))
    if existing:
        cfg = config_file if config_file is not None else existing.get("config_file", "")
        dash = dashboard_script if dashboard_script is not None else existing.get("dashboard_script", "")
        logs = log_dir if log_dir is not None else existing.get("log_dir", "")
    else:
        cfg = config_file or ""
        dash = dashboard_script or ""
        logs = log_dir or ""

    if not (cfg and dash and logs):
        detected = detect_project(root_path)
        if not cfg and detected.get("config_files"):
            cfg = _prefer_master_config(detected["config_files"])
        if not dash and detected.get("dashboard_script"):
            dash = detected["dashboard_script"]
        if not logs and detected.get("log_dir"):
            logs = detected["log_dir"]
    logs = logs or "logs"

    if existing:
        db.execute(
            "UPDATE quant_projects SET name = ?, config_file = ?, dashboard_script = ?, log_dir = ? WHERE id = ?",
            (name, cfg, dash, logs, existing["id"]),
        )
        db.log_operation("quant_register_project", {"name": name, "root_path": root_path}, {"updated": True})
        return db.query_one("SELECT * FROM quant_projects WHERE id = ?", (existing["id"],))  # type: ignore[return-value]

    cid = db.execute(
        "INSERT INTO quant_projects(name, root_path, config_file, dashboard_script, log_dir, is_active) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        (name, root_path, cfg, dash, logs),
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

    # The legacy "dashboard script" contract (``python <script> --json``) is gone;
    # FQA's single source of truth is ``python cli.py shadow`` →
    # ``outputs/shadow_status.json``. Keep the field (the DB column still exists
    # for backward compatibility) but stop auto-detecting a script.
    dashboard_script: str | None = None

    # A project "has" a quant surface when the FQA shadow/autopilot outputs exist
    # — no longer keyed off legacy ``phase10_dashboard.*`` / ``red_lines.json``.
    has_simulation_dashboard = bool(
        (root / "outputs" / "shadow_status.json").is_file()
        or (root / "outputs" / "s7_calibration.json").is_file()
        or (root / "outputs" / "autopilot_state.json").is_file()
    )

    return {
        "root_path": str(root),
        "config_files": config_files,
        "entry_points": entry_points,
        "log_dir": log_dir,
        "dashboard_script": dashboard_script or "",
        "has_simulation_dashboard": has_simulation_dashboard,
    }


def save_report_to_kb(title: str, text: str) -> dict[str, Any]:
    """Persist a quant report/result as a knowledge-base document."""
    return knowledge_base.ingest_text(title, text)


# --------------------------------------------------------------------------- #
# Red-line status
# --------------------------------------------------------------------------- #
def get_status() -> dict[str, Any]:
    """Return the D-track red-line status: one entry per shadow account plus
    an aggregate ``overall`` severity (worst across accounts).

    Only the D track remains (``D_5W``); the A/B/C ML cross-section accounts
    retired on 2026-09-08, so this normally holds a single entry. The red lines
    come from the FQA shadow-mode runs
    (``outputs/shadow_status_<name>.json``), which carry each line's own
    ``label`` / ``level`` / ``threshold`` / ``critical``. Before the first shadow
    run there is no status yet: report every line with ``level="unknown"`` and
    detail ``"shadow status not found"``. The top-level ``red_lines`` field keeps
    the first account's lines for backward compatibility.
    """
    timestamp = time.time()
    root = Path(_project_root())
    payloads: dict[str, dict[str, Any]] = {}
    for name in _shadow_account_names():
        for rel in (f"outputs/shadow_status_{name}.json", f"shadow_status_{name}.json"):
            path = root / rel
            if path.is_file():
                data = _parse_json_file(path)
                if isinstance(data, dict):
                    payloads[name] = data
                break
    if not payloads:
        # legacy single-account file, kept for backward compatibility
        for rel in ("outputs/shadow_status.json", "shadow_status.json"):
            path = root / rel
            if path.is_file():
                data = _parse_json_file(path)
                if isinstance(data, dict):
                    payloads["default"] = data
                break

    if not payloads:
        red_lines: list[dict[str, Any]] = [
            {
                "name": spec["name"],
                "label": spec["label"],
                "level": "unknown",
                "value": None,
                "threshold": spec.get("threshold"),
                "critical": spec.get("critical"),
                "detail": "shadow status not found",
            }
            for spec in _RED_LINE_SPECS
        ]
        return {
            "timestamp": timestamp,
            "overall": _overall([rl["level"] for rl in red_lines]),
            "red_lines": red_lines,
            "accounts": {},
            "source": "none",
        }

    accounts: dict[str, dict[str, Any]] = {}
    for name, payload in payloads.items():
        red_lines = [_extract_line(payload, spec) for spec in _RED_LINE_SPECS]
        accounts[name] = {
            "overall": _overall([rl["level"] for rl in red_lines]),
            "red_lines": red_lines,
            "last_trading_date": payload.get("last_trading_date") or payload.get("as_of"),
            "data_freshness_days": payload.get("data_freshness_days"),
            "equity": payload.get("equity"),
            "last_run": payload.get("last_run"),
            "account_name": payload.get("account_name", name),
        }
    first = next(iter(accounts))
    overall = _overall([a["overall"] for a in accounts.values()])
    return {
        "timestamp": timestamp,
        "overall": overall,
        "accounts": accounts,
        "red_lines": accounts[first]["red_lines"],
        "source": "D-track shadow",
    }


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


def _payload_timestamp(payload: Any) -> float | None:
    """Best-effort epoch seconds from the source data's own ``last_run``/``as_of``.

    FQA emits shadow status at most once a day; a ``time.time()`` timestamp would
    misleadingly show "just now" for day-old data. Prefer the run's own time.
    """
    if not isinstance(payload, dict):
        return None
    for key in ("last_run", "timestamp"):
        val = payload.get(key)
        if isinstance(val, (int, float)) and key == "timestamp":
            return float(val)
        if isinstance(val, str):
            try:
                return datetime.fromisoformat(val).timestamp()
            except ValueError:
                continue
    as_of = payload.get("as_of")
    if isinstance(as_of, str):
        try:
            return datetime.fromisoformat(as_of).timestamp()
        except ValueError:
            pass
    return None


def _build_status(data: Any, source: str, timestamp: float) -> dict[str, Any] | None:
    if isinstance(data, list):
        payload: Any = {"red_lines": data}
    elif isinstance(data, dict):
        payload = data
    else:
        return None

    red_lines = [_extract_line(payload, spec) for spec in _RED_LINE_SPECS]
    result: dict[str, Any] = {
        "timestamp": _payload_timestamp(payload) or timestamp,
        "overall": _overall([rl["level"] for rl in red_lines]),
        "red_lines": red_lines,
        "source": source,
    }
    # Surface the underlying data's own freshness metadata alongside the lines.
    if isinstance(payload, dict):
        for key in ("last_run", "as_of", "data_freshness_days"):
            if payload.get(key) is not None:
                result[key] = payload[key]
    return result


def _extract_line(payload: Any, spec: dict[str, Any]) -> dict[str, Any]:
    name = spec["name"]
    raw = _locate_entry(payload, name)

    value: Any = None
    level: Any = None
    threshold = spec.get("threshold")
    critical = spec.get("critical")
    label = spec["label"]
    detail = ""

    if isinstance(raw, dict):
        value = raw.get("value", raw.get("val", raw.get("current")))
        if value is None:
            value = raw.get("deviation", raw.get("pct"))
        level = raw.get("level", raw.get("status"))
        threshold = raw.get("threshold", raw.get("warning", threshold))
        critical = raw.get("critical", critical)
        # FQA's shadow status carries the canonical Chinese label; prefer it and
        # fall back to the spec so a label rename on the FQA side flows through.
        label = raw.get("label") or label
        detail = str(raw.get("detail", raw.get("message", "")))
    else:
        value = raw

    if level is None:
        level = _level_for(spec, value)

    return {
        "name": name,
        "label": label,
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
        # FQA emits flag lines as advisory warnings ("warning"/"ok"), never a
        # hard halt — keep the fallback consistent with that semantics.
        return "warning" if _truthy(value) else "ok"
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
    if any(level == "warning" for level in levels):
        return "warning"
    if any(level == "unknown" for level in levels):
        return "unknown"
    return "ok"


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
    cmd, root, project_id = _resolve_command(command_id, command)
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

    with _tracked_lock:
        _tracked[proc.pid] = {"pid": proc.pid, "project_id": project_id, "command": cmd, "root": root}

    publish("log_line", {"file": "command", "line": cmd, "level": "info"})
    db.log_operation(
        "quant_run_command",
        {"command_id": command_id, "command": cmd, "cwd": root},
        {"pid": proc.pid},
    )
    return {"pid": proc.pid, "command": cmd}


def _resolve_command(command_id: int | None, command: str | None) -> tuple[str, str, int | None]:
    if command:
        return str(command), _project_root(), _active_project_id()
    if command_id is None:
        raise ValueError("either command_id or command is required")
    row = db.query_one("SELECT * FROM quant_commands WHERE id = ?", (command_id,))
    if row is None:
        raise ValueError(f"command {command_id} not found")
    root = _project_root()
    project = db.query_one("SELECT root_path FROM quant_projects WHERE id = ?", (row["project_id"],))
    if project and project.get("root_path"):
        root = str(project["root_path"])
    return str(row["command"]), root, row["project_id"]


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


def stop_command(project_id: int | None = None, force: bool = False) -> dict[str, Any]:
    """Terminate tracked quant processes (optionally scoped to one project).

    Each tracked PID is terminated (then killed after a timeout) together with its
    child process tree. The real-time trader is PROTECTED unless ``force=True``:
    killing it mid-session leaves the D track unmonitored (the watchdog would
    relaunch it within 5 minutes, but the operator should say so explicitly).
    """
    with _tracked_lock:
        targets = [
            dict(info)
            for info in _tracked.values()
            if project_id is None or info.get("project_id") == project_id
        ]

    stopped: list[int] = []
    errors: list[int] = []
    protected: list[int] = []
    for info in targets:
        pid = info["pid"]
        cmd = str(info.get("command", "")).lower()
        if not force and "cli.py" in cmd and " live" in cmd:
            protected.append(pid)
            continue
        (stopped if _terminate_process_tree(pid) else errors).append(pid)

    with _tracked_lock:
        for pid in stopped + errors:
            _tracked.pop(pid, None)

    if stopped:
        publish("log_line", {"file": "command", "line": f"stopped processes: {stopped}", "level": "info"})
    db.log_operation(
        "quant_stop_command",
        {"project_id": project_id, "force": force},
        {"stopped": stopped, "errors": errors, "protected_live_trader": protected},
    )
    return {"stopped": stopped, "errors": errors, "protected_live_trader": protected}


def _terminate_process_tree(pid: int) -> bool:
    """Terminate ``pid`` and its children (terminate -> wait -> kill -> wait)."""
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True
    except psutil.AccessDenied:
        return False

    try:
        children = parent.children(recursive=True)
    except Exception:  # noqa: BLE001
        children = []

    procs = children + [parent]
    for proc in procs:
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            pass

    _, alive = psutil.wait_procs(procs, timeout=5)
    for proc in alive:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            pass

    _, alive = psutil.wait_procs(alive, timeout=3)
    return len(alive) == 0


def _pid_alive(pid: int) -> bool:
    try:
        psutil.Process(pid)
        return True
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True


def _reap_dead_tracked() -> None:
    """Drop tracked entries whose process has already exited."""
    with _tracked_lock:
        stale = [pid for pid in _tracked if not _pid_alive(pid)]
        for pid in stale:
            _tracked.pop(pid, None)


def _restart_tracked(project_id: int | None) -> list[int]:
    """Stop and re-spawn tracked commands (best-effort), returning the new PIDs."""
    _reap_dead_tracked()
    with _tracked_lock:
        infos = [
            dict(info)
            for info in _tracked.values()
            if project_id is None or info.get("project_id") == project_id
        ]

    stop_command(project_id)

    new_pids: list[int] = []
    for info in infos:
        try:
            proc = subprocess.Popen(
                info["command"],
                shell=True,
                cwd=info["root"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to restart tracked command %s: %s", info.get("command"), exc)
            continue
        with _tracked_lock:
            _tracked[proc.pid] = {
                "pid": proc.pid,
                "project_id": info.get("project_id"),
                "command": info["command"],
                "root": info["root"],
            }
        new_pids.append(proc.pid)
    return new_pids


# --------------------------------------------------------------------------- #
# Synchronous command runner + shadow/calibration outputs
# --------------------------------------------------------------------------- #
def run_project_command(command: str, timeout: int = 1800) -> dict[str, Any]:
    """Run ``command`` synchronously in the quant project root and capture output.

    Unlike :func:`run_command` (async ``Popen`` for the panel), this blocks until
    the process exits — used by the shadow/calibrate scheduler jobs. The child
    (FQA CLI) writes UTF-8, so capture with an explicit ``utf-8`` codec.
    """
    root = _project_root()
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "returncode": None,
            "stdout": (exc.stdout or "") if isinstance(exc.stdout, str) else "",
            "stderr": (str(exc.stderr) if exc.stderr else "") + f"\n(timeout after {timeout}s)",
            "timeout": True,
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("run_project_command failed")
        return {"command": command, "returncode": None, "stdout": "",
                "stderr": f"{type(exc).__name__}: {exc}", "timeout": False}
    return {
        "command": command,
        "returncode": proc.returncode,
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
        "timeout": False,
    }


# --------------------------------------------------------------------------- #
# PIT database (Docker Postgres) pre-flight
# --------------------------------------------------------------------------- #
# The daily autopilot/shadow/calibrate runs need the point-in-time store, which
# in production is a Dockerized Postgres (``fqa-pit-db``). Docker Desktop may not
# be running when the 17:30 scheduler fires (machine rebooted, or Docker closed),
# so ``ensure_pit_db_up`` brings the whole stack up *before* the CLI run instead
# of letting the run fail on an unreachable PIT store.

_DOCKER_DESKTOP_EXE = r"C:\Program Files\Docker\Docker\Docker Desktop.exe"
_PIT_CONTAINER = "fqa-pit-db"

#: Stage budget (seconds): daemon wait / compose up / container-health wait.
_DOCKER_DAEMON_WAIT = 240
_DOCKER_COMPOSE_TIMEOUT = 120
_DOCKER_COMPOSE_RETRIES = 3
_DOCKER_COMPOSE_RETRY_DELAY = 5
#: Total time the compose stage may keep retrying (the daemon is re-checked before
#: every attempt, so a flapping engine is waited out instead of burning the retries).
_DOCKER_COMPOSE_DEADLINE = 180
_PIT_HEALTH_WAIT = 120
#: Boot-hook total budget: keep calling ensure_pit_db_up until the stack is up.
#: 2026-09-13 measured a healthy cold start at ~17 s, so this is pure head-room for
#: a machine that is still busy starting up.
_PIT_AUTOSTART_DEADLINE = 900
_PIT_AUTOSTART_RETRY_DELAY = 20


def _run_docker(args: list[str], cwd: str | None = None, timeout: int = 60) -> subprocess.CompletedProcess | None:
    """Run ``docker <args>`` without a shell; ``None`` on spawn/OS error."""
    try:
        return subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=cwd,
        )
    except Exception:  # noqa: BLE001 — FileNotFoundError / TimeoutExpired / etc.
        return None


def _docker_daemon_up() -> bool:
    """True when the Docker engine answers ``docker info``."""
    proc = _run_docker(["info", "--format", "{{.ServerVersion}}"], timeout=15)
    return proc is not None and proc.returncode == 0


def _launch_docker_desktop() -> bool:
    """Start Docker Desktop if its launcher exists (best-effort, non-blocking)."""
    exe = os.environ.get("DOCKER_DESKTOP_EXE") or _DOCKER_DESKTOP_EXE
    if not Path(exe).is_file():
        return False
    try:
        flags = getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen(
            [exe],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
        )
        return True
    except Exception:  # noqa: BLE001
        logger.exception("failed to launch Docker Desktop %s", exe)
        return False


def _pit_container_healthy() -> bool:
    """True when ``fqa-pit-db`` reports ``healthy`` (its healthcheck is pg_isready)."""
    proc = _run_docker(
        ["inspect", "--format", "{{.State.Health.Status}}", _PIT_CONTAINER],
        timeout=15,
    )
    return proc is not None and proc.returncode == 0 and "healthy" in (proc.stdout or "")


def ensure_pit_db_up() -> dict[str, Any]:
    """Ensure the FQA PIT database is reachable before a daily run.

    The *real* success criterion is the container reporting ``healthy`` (its
    healthcheck is ``pg_isready``) — not that ``docker compose up -d`` returned 0.
    Right after a Docker Desktop restart the Linux engine can briefly answer 500
    on an image-inspect while the container is already coming back up via
    ``restart: unless-stopped`` (the 2026-08-22 calibration failure). So:

    1. Fast path — already healthy, don't touch Docker at all;
    2. Docker daemon — launch Docker Desktop if it isn't answering;
    3. ``docker compose up -d`` with a few retries for transient engine errors;
    4. container health — the final gate; a compose error is only reported if the
       container never becomes healthy.

    Never raises; returns ``{"ok", "stage", "detail"}`` so the caller can fail
    fast with an operator alert instead of a doomed CLI run. Each stage has its
    own budget (worst case ~5 min total).
    """
    root = _project_root()

    # 1. fast path — the DB is already serving, skip Docker entirely.
    if _pit_container_healthy():
        return {"ok": True, "stage": "already_up", "detail": "PIT 数据库已就绪"}

    # 2. daemon
    if not _docker_daemon_up():
        _launch_docker_desktop()
        stage_deadline = time.time() + _DOCKER_DAEMON_WAIT
        while not _docker_daemon_up() and time.time() < stage_deadline:
            time.sleep(3)
        if not _docker_daemon_up():
            return {"ok": False, "stage": "docker_daemon",
                    "detail": f"Docker 守护进程未在 {_DOCKER_DAEMON_WAIT}s 内就绪"}

    # 3. compose up, tolerating transient engine errors (image-inspect 500 while
    #    the engine warms up) and the restart policy already having done the work.
    #    The daemon is RE-CHECKED before every attempt: a Docker Desktop that was
    #    just launched can advertise then drop its pipe while the WSL VM boots
    #    (observed 2026-09-13: the daemon reported up, compose then failed with
    #    "dockerDesktopLinuxEngine ... cannot find the file specified", and the
    #    single 3-attempt burst was spent before the engine was really there).
    compose_err = ""
    compose_deadline = time.time() + _DOCKER_COMPOSE_DEADLINE
    attempt = 0
    while time.time() < compose_deadline:
        attempt += 1
        if not _docker_daemon_up():
            time.sleep(_DOCKER_COMPOSE_RETRY_DELAY)
            continue
        proc = _run_docker(["compose", "up", "-d"], cwd=root, timeout=_DOCKER_COMPOSE_TIMEOUT)
        if proc is not None and proc.returncode == 0:
            compose_err = ""
            break
        compose_err = (proc.stderr or "").strip() if proc else "docker compose 调用失败"
        if _pit_container_healthy():
            compose_err = ""
            break
        time.sleep(_DOCKER_COMPOSE_RETRY_DELAY)

    # 4. container health — the actual gate.
    stage_deadline = time.time() + _PIT_HEALTH_WAIT
    while not _pit_container_healthy() and time.time() < stage_deadline:
        time.sleep(3)
    if _pit_container_healthy():
        return {"ok": True, "stage": "healthy", "detail": f"PIT 数据库就绪（compose 尝试 {attempt} 次）"}
    detail = compose_err or f"{_PIT_CONTAINER} 未在 {_PIT_HEALTH_WAIT}s 内变为 healthy"
    return {"ok": False, "stage": "health", "detail": detail[-400:]}


#: Idempotence latch for the boot-time autostart (one thread per process).
_pit_autostart_started = False


def _pit_autostart_worker() -> None:
    """Bring the PIT stack up at boot and record the outcome (runs on a thread).

    Retries until the stack is healthy or ``_PIT_AUTOSTART_DEADLINE`` elapses: at
    boot the machine is often still busy (Docker Desktop itself just starting,
    WSL VM cold), and a single attempt that gives up would put the operator back
    in the 2026-09-11 situation — "the backend says ready, the data layer is not".
    ``ensure_pit_db_up`` returns immediately when the container is already healthy,
    so the loop costs nothing once the stack is up.
    """
    deadline = time.time() + _PIT_AUTOSTART_DEADLINE
    attempts = 0
    result: dict[str, Any] = {"ok": False, "stage": "not_attempted", "detail": ""}
    while True:
        attempts += 1
        try:
            result = ensure_pit_db_up()
        except Exception as exc:  # noqa: BLE001 — boot must never fail on this
            logger.exception("PIT autostart crashed")
            result = {"ok": False, "stage": "exception",
                      "detail": f"{type(exc).__name__}: {exc}"}
        if result.get("ok") or time.time() >= deadline:
            break
        time.sleep(_PIT_AUTOSTART_RETRY_DELAY)

    outcome = dict(result)
    outcome["attempts"] = attempts
    try:
        db.log_operation("quant_pit_autostart", {"trigger": "backend_startup"}, outcome)
    except Exception:  # noqa: BLE001
        logger.exception("could not log the PIT autostart outcome")
    if outcome.get("ok"):
        logger.info("PIT autostart: %s (stage=%s, attempts=%d)",
                    outcome.get("detail"), outcome.get("stage"), attempts)
    else:
        logger.warning("PIT autostart FAILED after %d attempt(s) at stage %s: %s",
                       attempts, outcome.get("stage"), outcome.get("detail"))


def ensure_pit_db_on_startup() -> dict[str, Any]:
    """PAICC boot hook: start Docker / the PIT database WITHOUT blocking startup.

    Why this exists (2026-09-11): Docker Desktop was not running when the machine
    was switched on, so the pre-open live check failed on ``connection refused``
    and the whole morning depended on someone noticing. The scheduled jobs already
    self-heal (``_ensure_pit_db_or_fail`` before every run, and the 09:25 live job
    retries until 09:56), but the panel was happily serving a "ready" backend whose
    data layer was down — the failure only appeared at the first job.

    So the backend brings the stack up itself as soon as it boots:

    * the work runs on a **daemon thread** — ``ensure_pit_db_up`` can legitimately
      spend minutes waiting for the Docker daemon, and the FastAPI lifespan must
      not block on that (the GUI would hang on launch);
    * the fast path inside ``ensure_pit_db_up`` returns immediately when the
      container is already healthy, so a normal boot costs one ``docker inspect``;
    * the result lands in ``operation_logs`` as ``quant_pit_autostart``, so
      「Docker 是后端自己拉起来的」 is visible instead of silent;
    * ``quant_pit_autostart=false`` disables it (an operator who manages Docker
      themselves should not have the backend fight them).

    Returns immediately; never raises.
    """
    global _pit_autostart_started
    if not settings.get_bool("quant_pit_autostart", True):
        return {"ok": True, "skipped": "quant_pit_autostart=false", "started": False}
    if _pit_autostart_started:
        return {"ok": True, "skipped": "已启动过", "started": False}
    _pit_autostart_started = True
    threading.Thread(target=_pit_autostart_worker, daemon=True,
                     name="pit-autostart").start()
    return {"ok": True, "started": True}


class OutputCorruptError(Exception):
    """An FQA output JSON exists but cannot be parsed as a dict (corrupt/empty)."""


def _read_output_json(root: Path, rels: tuple[str, ...]) -> dict[str, Any] | None:
    """Read the first existing FQA output JSON from ``rels``.

    ``None`` means *never run* (no file yet). Raises :class:`OutputCorruptError`
    when a file exists but fails to parse as a dict, so the panel can surface
    "output is corrupt" instead of misreporting "never run".
    """
    for rel in rels:
        path = root / rel
        if path.is_file():
            data = _parse_json_file(path)
            if isinstance(data, dict):
                return data
            raise OutputCorruptError(f"FQA output 损坏或不可读: {path.name}")
    return None


def read_shadow_status() -> dict[str, Any] | None:
    """Parse the FQA ``outputs/shadow_status.json`` (emitted by ``cli.py shadow``).

    The strategy-level config that drives the run (beta neutralization, rebalance
    cadence) is appended as a ``strategy`` field so the panel reflects the live
    factor setup, not just the numeric outputs.
    """
    root = Path(_project_root())
    data = _read_output_json(root, ("outputs/shadow_status.json", "shadow_status.json"))
    if data is not None:
        data.setdefault("strategy", _read_strategy_config(root))
    return data


def _read_strategy_config(root: Path) -> dict[str, Any]:
    """Read the alpha/paper strategy keys the FQA beta-neutralization change added.

    ``beta_neutralize`` / ``beta_lookback`` live under ``alpha_core``;
    ``rebalance_days`` under ``paper``. All are optional — a missing key just
    yields ``None`` / ``False`` so older configs keep working.
    """
    data = _load_yaml(root / "configs" / "master_config.yaml")
    if not isinstance(data, dict):
        data = {}
    alpha = data.get("alpha_core")
    paper = data.get("paper")
    alpha = alpha if isinstance(alpha, dict) else {}
    paper = paper if isinstance(paper, dict) else {}
    return {
        "beta_neutralize": bool(alpha.get("beta_neutralize", False)),
        "beta_lookback": alpha.get("beta_lookback"),
        "rebalance_days": paper.get("rebalance_days"),
    }


def read_s7_calibration() -> dict[str, Any] | None:
    """Parse the FQA ``outputs/s7_calibration.json`` (emitted by ``cli.py calibrate``)."""
    return _read_output_json(
        Path(_project_root()), ("outputs/s7_calibration.json", "s7_calibration.json")
    )


def read_autopilot_state() -> dict[str, Any] | None:
    """Parse the FQA ``outputs/autopilot_state.json`` (emitted by ``cli.py autopilot``).

    Carries the persisted kill-switch operating mode (normal / de_risk / halt), the
    gross multiplier, the escalation reason and the cadence bookkeeping (last
    calibrate / monitor / mine). ``None`` before the first autopilot run.
    """
    return _read_output_json(
        Path(_project_root()), ("outputs/autopilot_state.json", "autopilot_state.json")
    )


def read_autopilot_states() -> dict[str, Any]:
    """Per-account autopilot states: ``{name: state}`` from
    ``outputs/autopilot_state_<name>.json`` (D track only since 2026-09-08),
    falling back to the legacy single-account ``outputs/autopilot_state.json``.
    """
    root = Path(_project_root())
    names = _shadow_account_names()
    out: dict[str, Any] = {}
    for name in names:
        state = _read_output_json(root, (f"outputs/autopilot_state_{name}.json",))
        if state is not None:
            out[name] = state
    if not out:
        legacy = read_autopilot_state()
        if legacy is not None:
            out["default"] = legacy
    return out


# --------------------------------------------------------------------------- #
# Shadow accounts — per-account status + trade records.
# Historical note: this used to serve the multi-capital tracks
# (A_200W / B_10W / C_5W). Those ML cross-section tracks retired 2026-09-08 and
# ``shadow.accounts`` now lists only ``D_5W`` (alpha_source: pullback); the code
# still iterates the config list, so a re-added account would surface again.
# --------------------------------------------------------------------------- #
def _shadow_account_names() -> list[str]:
    """Account names from ``shadow.accounts`` in the master config (fallback: legacy).

    Sorted by ``priority`` DESC so the live D track (100) leads the panel tabs
    and the status payloads — matching the FQA run order.
    """
    root = Path(_project_root())
    cfg = _load_yaml(root / "configs" / "master_config.yaml")
    accounts = (cfg or {}).get("shadow", {}).get("accounts") or []
    accounts = sorted(accounts, key=lambda a: int(a.get("priority", 0) or 0), reverse=True)
    names = [str(a.get("name")) for a in accounts if a.get("name")]
    return names


def read_shadow_accounts() -> dict[str, Any]:
    """Per-account shadow payloads: ``{name: {status, report, autopilot}}``.

    When no ``shadow.accounts`` are configured, returns ``{"default": {...}}``
    with the legacy single-account files (backward compatible).
    """
    root = Path(_project_root())
    names = _shadow_account_names() or [""]
    out: dict[str, Any] = {}
    for name in names:
        suffix = f"_{name}" if name else ""
        status = _read_output_json(
            root, (f"outputs/shadow_status{suffix}.json",)
        )
        report = _read_report_file(root / f"outputs/shadow_report{suffix}.md")
        state = _read_output_json(
            root, (f"outputs/autopilot_state{suffix}.json",)
        )
        entry: dict[str, Any] = {"name": name or "default"}
        if status is not None:
            entry["status"] = status
        if report:
            entry["report"] = report
        if state is not None:
            entry["autopilot"] = state
        out[name or "default"] = entry
    return out


def _read_report_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def read_trade_records(account: str = "", limit: int = 200, date: str | None = None) -> dict[str, Any]:
    """Fills + daily equity from one account's shadow ledger.

    ``account`` "" reads the legacy ledger; otherwise ``shadow_ledger_<name>.sqlite``.
    ``date`` (ISO) filters fills to one trading day (the daily-report trades view).
    Each SELL fill carries ``entry_price`` — the position's moving-average buy
    price at the moment of that sell (same semantics as FQA ``enrich_positions``),
    so the panel can show 买入价/卖出价 side by side.
    """
    import sqlite3

    root = Path(_project_root())
    name = str(account or "").strip()
    if name and not _ACCOUNT_NAME_RE.match(name):
        raise ValueError(f"invalid account name: {name!r}")
    suffix = f"_{name}" if name else ""
    ledger = root / f"outputs/shadow_ledger{suffix}.sqlite"
    if not ledger.is_file():
        return {"account": account, "fills": [], "days": [], "note": "ledger not found"}
    con = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        # Full history in seq order: the moving-average entry price of every
        # sell depends on ALL preceding fills of that symbol, so compute the
        # cost basis first and only then apply the date/limit window.
        rows = con.execute(
            "SELECT seq, date, time, symbol, side, shares, price, commission, notional "
            "FROM fills ORDER BY seq"
        ).fetchall()
        basis: dict[str, float] = {}
        signed: dict[str, float] = {}
        enriched: list[dict[str, Any]] = []
        for r in rows:
            sym = str(r["symbol"])
            qty = float(r["shares"])
            px = float(r["price"])
            side = str(r["side"] or "").lower()
            s = signed.get(sym, 0.0)
            b = basis.get(sym, 0.0)
            entry = None
            if s != 0.0 and side == "sell":
                entry = b / s  # avg buy price BEFORE this sell is applied
            if s == 0.0:
                signed[sym] = qty
                basis[sym] = qty * px
            elif (qty > 0) == (s > 0):
                signed[sym] = s + qty
                basis[sym] = b + qty * px
            else:
                avg = b / s
                closing = min(abs(qty), abs(s))
                sign = 1.0 if s > 0 else -1.0
                new_b = b - sign * closing * avg
                new_s = s + qty
                remaining = abs(qty) - closing
                if remaining > 0:
                    open_sign = 1.0 if qty > 0 else -1.0
                    new_s = open_sign * remaining
                    new_b = open_sign * remaining * px
                signed[sym] = new_s
                basis[sym] = new_b
            d = dict(r)
            d["entry_price"] = round(float(entry), 4) if entry is not None else None
            enriched.append(d)

        if date:
            fills = [d for d in enriched if d["date"] == date][-limit:][::-1]
        else:
            fills = enriched[-limit:][::-1]
        days = con.execute(
            "SELECT date, cash, equity, gross_exposure, n_fills, commission "
            "FROM daily_state ORDER BY date DESC LIMIT 60"
        ).fetchall()
        return {"account": account or "default", "fills": fills, "days": [dict(r) for r in days]}
    finally:
        con.close()


def live_trader_alive() -> bool:
    """True when the FQA real-time trader process is running (audit P-4).

    Reads ``outputs/live_<account>.pid`` and validates the command line — a
    recycled pid must not look like a live trader. Used by the scheduler's
    watchdog to relaunch a trader that died mid-session.
    """
    root = Path(_project_root())
    name = _live_account_name(root, "")
    pid_file = root / "outputs" / f"live_{name}.pid"
    if not pid_file.is_file():
        return False
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    try:
        import psutil
    except ImportError:
        try:
            os.kill(pid, 0)  # noqa: S101 — existence probe only
            return True
        except OSError:
            return False
    try:
        proc = psutil.Process(pid)
        cmd = " ".join(proc.cmdline()).lower()
        return "cli.py" in cmd and "live" in cmd.split()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False


def read_live_status(account: str = "") -> dict[str, Any] | None:
    """Parse the FQA real-time intraday trader status ``outputs/live_<account>.json``.

    ``account`` "" resolves to ``live.account`` in the master config (default
    ``D_5W``). ``None`` means the trader has not run yet (no file); the payload
    carries minute-precision ``ts``, live equity/cash/invested% and per-position
    P&L refreshed at every poll — never backfilled from past timestamps.
    """
    root = Path(_project_root())
    name = _live_account_name(root, account)
    if not _ACCOUNT_NAME_RE.match(name):
        raise ValueError(f"invalid account name: {name!r}")
    data = _read_output_json(root, (f"outputs/live_{name}.json",))
    if data is not None:
        data.setdefault("account", name)
    return data


#: Account names accepted by the per-account output readers. FQA account names
#: are plain identifiers (``D_5W``); anything else is rejected so a caller cannot
#: escape ``outputs/`` with ``../`` or an absolute path.
_ACCOUNT_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _newest_output(
    root: Path, pattern: str, canonical: str | None = None
) -> tuple[dict[str, Any], str, bool, list[str]] | None:
    """Read a forward-period artifact → ``(payload, relpath, fallback, corrupt)``.

    ``canonical`` (when given) is tried FIRST, before mtime order: FQA's
    scheduled jobs read/write exactly that path, while the same directory also
    holds hand-named copies (``forward_health_shakedown_20260909.json`` —
    FQA itself labels it "not the forward window, pipeline shakedown only").
    Picking purely by mtime would silently render such a shakedown run as the
    real forward window. ``fallback=True`` means the payload came from a
    non-canonical file, so the panel can label it 试跑/非前向窗口.

    A file that exists but does not parse as a dict is SKIPPED — its name is
    collected in ``corrupt`` and the next candidate is tried: one broken file
    must not blank the whole 「前向期」 bundle (gate AND candidate AND prereg).
    Only when EVERY candidate is corrupt does this raise
    :class:`OutputCorruptError`; ``None`` means nothing matched (never ran).
    """
    canonical_path = root / canonical if canonical else None
    paths: list[Path] = []
    if canonical_path is not None and canonical_path.is_file():
        paths.append(canonical_path)
    hits = sorted(root.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    paths.extend(path for path in hits if path != canonical_path)

    corrupt: list[str] = []
    for path in paths:
        rel = str(path.relative_to(root)).replace("\\", "/")
        data = _parse_json_file(path)
        if isinstance(data, dict):
            fallback = canonical is not None and rel != canonical
            return data, rel, fallback, corrupt
        corrupt.append(rel)
    if corrupt:
        raise OutputCorruptError(f"FQA output 损坏或不可读: {', '.join(corrupt)}")
    return None


def read_forward_health() -> dict[str, Any] | None:
    """Forward-period RISK gate artifact, canonical path first.

    The canonical ``outputs/forward/forward_health.json`` is what the scheduled
    health job writes; only when it is absent (or unreadable) does the reader
    fall back to the newest ``forward_health*.json``, flagged ``fallback=True``
    so the panel can mark that artifact 试跑/非前向窗口. ``corrupt`` lists any
    candidate that existed but could not be parsed, so a single broken file
    shows a warning instead of an empty screen.

    ``None`` means the gate has never been evaluated. The payload carries the
    hard/soft verdict, the measured metrics (tracking error, cost, violations,
    availability, freshness, coverage) and the provenance block.
    """
    hit = _newest_output(
        Path(_project_root()),
        "outputs/forward/forward_health*.json",
        canonical="outputs/forward/forward_health.json",
    )
    if hit is None:
        return None
    data, rel, fallback, corrupt = hit
    data.setdefault("artifact", rel)
    data["fallback"] = fallback
    data["corrupt"] = corrupt
    return data


def read_forward_paired() -> dict[str, Any] | None:
    """Forward-candidate PAIRED comparison, canonical path first.

    Same canonical-first rule as :func:`read_forward_health`: the candidate job
    writes ``outputs/forward/paired_<rule>.json`` for the tracked rule, and only a
    missing (or unreadable) canonical file makes the reader fall back to the
    newest ``paired_*.json`` — a stale artifact from a previous candidate must
    never outrank the live one just because it was touched later.

    The canonical name follows the TRACKED candidate, which changed on 2026-09-10
    (``atr_1p0_25_40`` → ``atr_1p0_25_35``, see FQA docs/FORWARD_PROTOCOL.md §2).

    Record-only: the payload states the pre-registered switch rule and whether
    the candidate currently warrants a switch (``verdict`` = switch | hold).
    """
    hit = _newest_output(
        Path(_project_root()),
        "outputs/forward/paired_*.json",
        canonical="outputs/forward/paired_atr_1p0_25_35.json",
    )
    if hit is None:
        return None
    data, rel, fallback, corrupt = hit
    data.setdefault("artifact", rel)
    data["fallback"] = fallback
    data["corrupt"] = corrupt
    return data


def read_forward_prereg() -> list[dict[str, Any]]:
    """All pre-registration records (append-only, newest ``frozen_at`` first).

    A record that cannot be parsed is LISTED with ``error: unreadable`` rather
    than dropped: silently omitting it would hide the fact that a rule was
    frozen (and possibly superseded) from the panel.
    """
    root = Path(_project_root())
    out: list[dict[str, Any]] = []
    for path in sorted(root.glob("outputs/forward/prereg/prereg_*.json")):
        data = _parse_json_file(path)
        if not isinstance(data, dict):
            out.append({"rule_id": path.stem, "error": "unreadable"})
            continue
        data.setdefault("artifact", str(path.relative_to(root)).replace("\\", "/"))
        out.append(data)
    out.sort(key=lambda r: str(r.get("frozen_at", "")), reverse=True)
    return out


def read_forward() -> dict[str, Any]:
    """Forward-period bundle for the panel: gate + candidate + pre-registrations."""
    health = read_forward_health()
    paired = read_forward_paired()
    # the paired artifact nests its result under ``paired`` (provenance is a
    # sibling key), so the verdict lookup has to go one level down
    paired_result = (paired or {}).get("paired") or {}
    return {
        "health": health,
        "paired": paired,
        "prereg": read_forward_prereg(),
        "gate_verdict": ((health or {}).get("gate") or {}).get("verdict"),
        "candidate_verdict": paired_result.get("verdict"),
    }


def _live_account_name(root: Path, account: str = "") -> str:
    """Resolve an account name: the explicit argument, else ``live.account``.

    ``live.account`` defaults to ``D_5W`` — the only track left after the
    2026-09-08 A/B/C retirement.
    """
    name = str(account or "").strip()
    if name:
        return name
    cfg = _load_yaml(root / "configs" / "master_config.yaml")
    live_cfg = cfg.get("live") if isinstance(cfg, dict) else None
    live_cfg = live_cfg if isinstance(live_cfg, dict) else {}
    return str(live_cfg.get("account") or "D_5W")


def _preclose_age_minutes(date_value: Any, ts_value: Any, path: Path) -> int | None:
    """Minutes since the order list was decided (``None`` when undeterminable).

    Uses the payload's own ``date`` + ``ts``; falls back to the file mtime when
    the decision timestamp is missing/unparsable. Never negative.
    """
    candidates: list[str] = []
    if isinstance(date_value, str) and date_value.strip():
        date_txt = date_value.strip()
        if isinstance(ts_value, str) and ts_value.strip():
            candidates.append(f"{date_txt} {ts_value.strip()}")
        candidates.append(date_txt)
    for candidate in candidates:
        try:
            decided = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        return max(0, int(round((datetime.now() - decided).total_seconds() / 60.0)))
    try:
        return max(0, int(round((time.time() - path.stat().st_mtime) / 60.0)))
    except OSError:
        return None


def read_preclose_orders(account: str = "") -> dict[str, Any] | None:
    """Read FQA's ``outputs/preclose_orders_<account>.json`` (D-track 14:50 list).

    ``cli.py preclose`` decides the closing-auction order list from 14:50-known
    data (provisional minute bars + T-1 ranks) and persists it; the 15:00 auction
    close then fills exactly that list. These are **simulated** decisions on the
    D shadow book — not broker orders.

    ``account`` "" resolves to ``live.account`` (default ``D_5W``); the name must
    match ``[A-Za-z0-9_]+`` (raises ``ValueError`` otherwise, so a caller cannot
    traverse out of ``outputs/``). Returns ``None`` when the file does not exist
    (the router maps that to 404); raises :class:`OutputCorruptError` when it
    exists but cannot be parsed.

    Two derived fields make the freshness explicit for the panel: ``stale``
    (the payload's ``date`` is not today — a historical list must never be read
    as today's) and ``age_minutes`` (minutes since the decision timestamp).
    """
    root = Path(_project_root())
    name = _live_account_name(root, account)
    if not _ACCOUNT_NAME_RE.match(name):
        raise ValueError(f"invalid account name: {name!r}")
    path = root / "outputs" / f"preclose_orders_{name}.json"
    if not path.is_file():
        return None
    data = _parse_json_file(path)
    if not isinstance(data, dict):
        raise OutputCorruptError(f"FQA output 损坏或不可读: {path.name}")
    orders = data.get("orders")
    return {
        "date": data.get("date"),
        "ts": data.get("ts"),
        "orders": orders if isinstance(orders, list) else [],
        "note": str(data.get("note") or ""),
        "stale": str(data.get("date") or "") != datetime.now().strftime("%Y-%m-%d"),
        "age_minutes": _preclose_age_minutes(data.get("date"), data.get("ts"), path),
    }


# --------------------------------------------------------------------------- #
# Config file read / write
# --------------------------------------------------------------------------- #
def get_config_text(project_id: int | None = None) -> dict[str, Any]:
    """Read the configured ``config_file`` of a project (or the active project)."""
    project = _get_project(project_id)
    if project is None:
        raise ValueError("no quant project found")
    root = str(project["root_path"])
    config_file = str(project.get("config_file") or "").strip()
    if not config_file:
        raise ValueError("project has no config_file configured")
    path = _resolve_path(root, config_file)
    content = ""
    if path.is_file():
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to read config %s: %s", path, exc)
    return {
        "project_id": project["id"],
        "config_file": config_file,
        "path": str(path),
        "content": content,
    }


def save_config(project_id: int | None, content: str) -> dict[str, Any]:
    """Write ``content`` back to the project's config file and restart tracked processes."""
    project = _get_project(project_id)
    if project is None:
        raise ValueError("no quant project found")
    root = str(project["root_path"])
    config_file = str(project.get("config_file") or "").strip()
    if not config_file:
        raise ValueError("project has no config_file configured")
    path = _resolve_path(root, config_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

    restarted = _restart_tracked(project["id"])
    db.log_operation(
        "quant_save_config",
        {"project_id": project["id"], "config_file": config_file},
        {"path": str(path), "restarted": restarted},
    )
    publish(
        "quant_config_saved",
        {"project_id": project["id"], "config_file": config_file, "restarted": restarted},
    )
    return {
        "project_id": project["id"],
        "config_file": config_file,
        "path": str(path),
        "restarted": restarted,
    }


# --------------------------------------------------------------------------- #
# Path helpers
# --------------------------------------------------------------------------- #
def _get_project(project_id: int | None = None) -> dict[str, Any] | None:
    """Return a project row by id, or the active project when id is None."""
    if project_id is not None:
        return db.query_one("SELECT * FROM quant_projects WHERE id = ?", (project_id,))
    return db.query_one("SELECT * FROM quant_projects WHERE is_active = 1 ORDER BY id ASC LIMIT 1")


def _active_project_id() -> int | None:
    row = db.query_one("SELECT id FROM quant_projects WHERE is_active = 1 ORDER BY id ASC LIMIT 1")
    return row["id"] if row else None


def _resolve_path(root: str, rel: str) -> Path:
    p = Path(rel)
    if p.is_absolute():
        return p
    return Path(root) / p


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
# Red-line history (persisted snapshots for drift / trend charts, per account)
# --------------------------------------------------------------------------- #
def _migrate_redline_history() -> None:
    """Add the ``account`` column to ``quant_redline_history`` if missing."""
    try:
        cols = db.query("PRAGMA table_info(quant_redline_history)")
    except Exception:  # noqa: BLE001 — table missing (fresh DB) is handled by the schema init
        return
    names = {str(r.get("name", "")) for r in cols}
    if "account" not in names:
        try:
            db.execute("ALTER TABLE quant_redline_history ADD COLUMN account TEXT NOT NULL DEFAULT ''")
            logger.info("quant_redline_history: added account column")
        except Exception as exc:  # noqa: BLE001
            logger.warning("quant_redline_history migration failed: %s", exc)


def _latest_db_signature(account: str) -> tuple[tuple[str, str, str], ...] | None:
    """Reconstruct the signature of the most recent snapshot already in the DB.

    All rows of one snapshot share a ``ts``; this lets a freshly-started process
    skip re-writing a snapshot that was persisted before the restart.
    """
    rows = db.query(
        "SELECT name, level, value FROM quant_redline_history "
        "WHERE account = ? AND ts = (SELECT MAX(ts) FROM quant_redline_history WHERE account = ?) "
        "ORDER BY id",
        (account, account),
    )
    if not rows:
        return None
    return tuple(
        sorted((str(r["name"]), str(r["level"]), str(r["value"])) for r in rows)
    )


def _append_redline_history(account: str, account_status: dict[str, Any]) -> None:
    """Persist a snapshot of one account's red lines when their values change.

    The shadow data refreshes at most once a day, so keying on the value
    signature (rather than the 10s poll tick) yields exactly the daily drift
    history the panel needs — without spamming a row every poll. On the first
    poll of a process we seed the in-memory signature from the DB so a restart
    does not re-write the latest snapshot.
    """
    global _last_history_signature
    red_lines = account_status.get("red_lines", [])
    if not red_lines:
        return
    signature = tuple(
        sorted((str(rl.get("name")), str(rl.get("level")), str(rl.get("value")))
               for rl in red_lines)
    )
    if _last_history_signature is None:
        _last_history_signature = {}
    if account not in _last_history_signature:
        db_sig = _latest_db_signature(account)
        _last_history_signature[account] = (
            f"{account}:{db_sig}" if db_sig is not None else None
        )
    sig_key = f"{account}:{signature}"
    if sig_key == _last_history_signature.get(account):
        return
    _last_history_signature[account] = sig_key
    ts = float(account_status.get("timestamp") or time.time())
    for rl in red_lines:
        value = rl.get("value")
        db.execute(
            "INSERT INTO quant_redline_history(ts, source, name, label, level, value, detail, account) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ts,
                str(account_status.get("source", "")),
                str(rl.get("name", "")),
                str(rl.get("label", "")),
                str(rl.get("level", "")),
                None if value is None else str(value),
                str(rl.get("detail", "")),
                account,
            ),
        )


def redline_history(limit: int = 200, account: str = "") -> list[dict[str, Any]]:
    """Return the latest ``limit`` persisted red-line snapshots for ``account``,
    oldest first.

    ``account`` "" returns the legacy rows written before the single-D-track
    convergence. A subquery first grabs
    the newest ``limit`` rows (by ts desc), then the outer sort flips them back to
    ascending for trend charts.
    """
    rows = db.query(
        "SELECT * FROM ("
        "  SELECT * FROM quant_redline_history WHERE account = ? ORDER BY ts DESC, id DESC LIMIT ?"
        ") ORDER BY ts ASC, id ASC",
        (account, limit),
    )
    for r in rows:
        v = r.get("value")
        if v is not None:
            try:
                r["value"] = float(v)
            except (TypeError, ValueError):
                pass  # keep the raw string (e.g. boolean "True"/"False")
    return rows


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

    _migrate_redline_history()
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
        for name, command, description in (
            ("CLI 帮助", "python cli.py --help", "量化项目 CLI 帮助"),
            ("回测", "python cli.py backtest --help", "回测因子公式（PIT 数据）"),
            ("影子模式", "python cli.py shadow --help", "影子模式 — 逐日记录目标持仓与 PnL"),
            ("自动闭环", "python cli.py autopilot --help", "自动闭环 — shadow → 风险闸门 → 回校/监控"),
        ):
            db.execute(
                "INSERT INTO quant_commands(project_id, name, command, description) VALUES (?, ?, ?, ?)",
                (project["id"], name, command, description),
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
            publish("quant_processes", monitor_processes())

            status = get_status()
            overall = status.get("overall")
            accounts = status.get("accounts", {})
            for account, acc in accounts.items():
                acc["timestamp"] = status.get("timestamp")
                acc["source"] = acc.get("source") or status.get("source")
                _append_redline_history(account, acc)
            red_lines = status.get("red_lines", [])

            criticals = {rl["name"] for rl in red_lines if rl.get("level") == "critical"}
            new_criticals = criticals - _last_criticals

            if overall != _last_overall and overall != "unknown":
                names = [rl["name"] for rl in red_lines if rl.get("level") in ("warning", "critical")]
                publish(
                    "red_line_alert",
                    {
                        "title": f"红线状态变化: {overall}",
                        "message": _summarize(status),
                        "names": names,
                    },
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
                        "names": [name],
                    },
                )

            _last_overall = overall
            _last_criticals = criticals
        except Exception:  # noqa: BLE001
            logger.exception("quant status poll failed")
        time.sleep(10)


def _summarize(status: dict[str, Any]) -> str:
    accounts = status.get("accounts") or {}
    parts: list[str] = []
    for name, acc in accounts.items():
        acc_parts = [f"{rl.get('label', rl.get('name'))}: {rl.get('level')}"
                     for rl in acc.get("red_lines", [])]
        parts.append(f"[{name}] " + ("; ".join(acc_parts) or "no red lines"))
    if not parts:
        parts = [f"{rl.get('label', rl.get('name'))}: {rl.get('level')}"
                 for rl in status.get("red_lines", [])]
    return " | ".join(parts) or "no red lines"

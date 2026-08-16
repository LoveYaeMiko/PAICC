"""System monitoring service.

Collects CPU / memory / disk / network / GPU / process statistics using
``psutil``, and exposes process and Windows power-plan management. The optional
``GPUtil`` dependency is imported lazily so the backend works without it.

A background loop (started idempotently via :func:`start`) pushes
``system_stats`` events to the WebSocket bus every 2 seconds and throttled
``system_alert`` events when CPU or any disk is over 90%.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Any

import psutil

from app.config import settings
from app.ws import publish

logger = logging.getLogger(__name__)

#: Module-level network-rate state (previous counters + timestamp).
_net_lock = threading.Lock()
_prev_net: dict[str, float] = {"bytes_sent": 0.0, "bytes_recv": 0.0, "timestamp": 0.0}

#: Module-level disk-I/O rate state (previous counters + timestamp).
_disk_lock = threading.Lock()
_prev_disk: dict[str, float] = {"read_bytes": 0.0, "write_bytes": 0.0, "timestamp": 0.0}

#: Module-level guard so start() is idempotent.
_started = False

#: Common power-plan display names -> well-known Windows GUIDs.
_POWER_PLAN_GUIDS: dict[str, str] = {
    "节能": "a1841308-3541-4fab-bc81-f71556f20b4a",
    "平衡": "381b4222-f694-41f0-9685-ff5bb260df2e",
    "高性能": "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c",
    "卓越性能": "e9a42b02-d5df-448d-aa00-03f14749eb61",
    "power saver": "a1841308-3541-4fab-bc81-f71556f20b4a",
    "balanced": "381b4222-f694-41f0-9685-ff5bb260df2e",
    "high performance": "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c",
    "ultimate performance": "e9a42b02-d5df-448d-aa00-03f14749eb61",
}

_GUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_NAME_RE = re.compile(r"\(([^)]*)\)")

#: Cache of the last ``powercfg /list`` result.
_power_plan_cache: dict[str, Any] = {"ts": 0.0, "plans": []}
_POWER_PLAN_CACHE_TTL = 8.0


def get_system_stats() -> dict[str, Any]:
    """Return a snapshot of CPU, memory, disk, network, GPU and uptime.

    Network per-second rates are computed from module-level counters against the
    previous call, so consecutive calls yield meaningful throughput values.
    """
    cpu_percent = round(psutil.cpu_percent(interval=None), 1)
    cpu_per_core = [round(v, 1) for v in psutil.cpu_percent(interval=None, percpu=True)]
    cpu_count = psutil.cpu_count() or 0

    freq = psutil.cpu_freq()
    cpu_freq: float | None = round(freq.current, 1) if (freq and freq.current) else None

    mem = psutil.virtual_memory()
    memory = {
        "total": mem.total,
        "used": mem.used,
        "available": mem.available,
        "percent": round(mem.percent, 1),
    }

    disk: list[dict[str, Any]] = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue
        disk.append(
            {
                "device": part.device,
                "mountpoint": part.mountpoint,
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
                "percent": round(usage.percent, 1),
            }
        )

    counters = psutil.net_io_counters()
    now = time.time()
    with _net_lock:
        prev = _prev_net
        if prev["timestamp"]:
            dt = now - prev["timestamp"]
            if dt > 0:
                sent_per_sec = (counters.bytes_sent - prev["bytes_sent"]) / dt
                recv_per_sec = (counters.bytes_recv - prev["bytes_recv"]) / dt
            else:
                sent_per_sec = recv_per_sec = 0.0
        else:
            sent_per_sec = recv_per_sec = 0.0
        _prev_net["bytes_sent"] = counters.bytes_sent
        _prev_net["bytes_recv"] = counters.bytes_recv
        _prev_net["timestamp"] = now

    net = {
        "bytes_sent": counters.bytes_sent,
        "bytes_recv": counters.bytes_recv,
        "sent_per_sec": round(sent_per_sec, 1),
        "recv_per_sec": round(recv_per_sec, 1),
    }

    try:
        disk_io_counters = psutil.disk_io_counters()
    except (OSError, AttributeError):
        disk_io_counters = None

    with _disk_lock:
        prev_disk = _prev_disk
        if disk_io_counters is not None:
            read_bytes = disk_io_counters.read_bytes
            write_bytes = disk_io_counters.write_bytes
            if prev_disk["timestamp"]:
                dt = now - prev_disk["timestamp"]
                if dt > 0:
                    read_per_sec = (read_bytes - prev_disk["read_bytes"]) / dt
                    write_per_sec = (write_bytes - prev_disk["write_bytes"]) / dt
                else:
                    read_per_sec = write_per_sec = 0.0
            else:
                read_per_sec = write_per_sec = 0.0
            _prev_disk["read_bytes"] = read_bytes
            _prev_disk["write_bytes"] = write_bytes
            _prev_disk["timestamp"] = now
        else:
            read_bytes = write_bytes = 0
            read_per_sec = write_per_sec = 0.0

    disk_io = {
        "read_bytes": read_bytes,
        "write_bytes": write_bytes,
        "read_per_sec": round(read_per_sec, 1),
        "write_per_sec": round(write_per_sec, 1),
    }

    gpu = _gpu_stats()

    uptime_seconds = round(time.time() - psutil.boot_time(), 1)

    return {
        "cpu_percent": cpu_percent,
        "cpu_per_core": cpu_per_core,
        "cpu_count": cpu_count,
        "cpu_freq": cpu_freq,
        "memory": memory,
        "disk": disk,
        "net": net,
        "disk_io": disk_io,
        "gpu": gpu,
        "uptime_seconds": uptime_seconds,
    }


#: Module-level GPU cache (``nvidia-smi`` is a subprocess; don't spawn it on every
#: 2-second monitor tick or page-load status call).
_gpu_cache: dict[str, Any] = {"timestamp": 0.0, "value": []}
_GPU_CACHE_TTL = 10.0


def _gpu_stats() -> list[dict[str, Any]]:
    """Read GPU metrics via ``nvidia-smi``, falling back to GPUtil, else ``[]``.

    Results are cached briefly so the 2-second monitor loop and page-load status
    calls don't each pay the cost of spawning ``nvidia-smi``.
    """
    now = time.time()
    if now - _gpu_cache["timestamp"] < _GPU_CACHE_TTL:
        return _gpu_cache["value"]
    result = _gpu_stats_nvidia_smi()
    if not result:
        result = _gpu_stats_gputil()
    _gpu_cache["timestamp"] = now
    _gpu_cache["value"] = result
    return result


def _to_float(value: str) -> float | None:
    """Parse a numeric token, tolerating ``[N/A]`` / empty strings."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _gpu_stats_nvidia_smi() -> list[dict[str, Any]]:
    """Query GPU metrics with ``nvidia-smi`` (no third-party dependencies)."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        proc = subprocess.run(
            [
                exe,
                "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=4,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []

    result: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        name, temp, util, mem_used, mem_total = parts[:5]
        result.append(
            {
                "name": name,
                "load": _to_float(util) or 0.0,
                "temperature": _to_float(temp),
                "memory_used": int(_to_float(mem_used) or 0.0),
                "memory_total": int(_to_float(mem_total) or 0.0),
            }
        )
    return result


def _gpu_stats_gputil() -> list[dict[str, Any]]:
    """Legacy fallback via GPUtil (broken on Python >= 3.12 without distutils)."""
    try:
        import GPUtil  # lazy optional dependency
    except ImportError:
        return []

    try:
        result: list[dict[str, Any]] = []
        for g in GPUtil.getGPUs():
            result.append(
                {
                    "name": g.name,
                    "load": round(float(g.load) * 100.0, 1),
                    "temperature": getattr(g, "temperature", None),
                    "memory_used": getattr(g, "memoryUsed", 0),
                    "memory_total": getattr(g, "memoryTotal", 0),
                }
            )
        return result
    except Exception:  # noqa: BLE001 — nvidia-smi missing, no GPU, etc.
        return []


def get_processes(sort_by: str = "cpu") -> list[dict[str, Any]]:
    """Return up to 200 processes, sorted descending by the requested key.

    ``sort_by`` may be ``cpu``, ``memory`` or ``disk`` (all descending) or
    ``name`` (ascending, case-insensitive). Per-process *network* I/O is not
    available via psutil (``io_counters`` only reports disk read/write bytes),
    so there is deliberately no ``network`` sort option here.
    """
    key = sort_by.strip().lower() if sort_by else "cpu"
    attrs = [
        "pid", "name", "username", "cpu_percent", "memory_percent",
        "memory_info", "create_time", "exe", "cmdline", "status",
    ]
    processes: list[dict[str, Any]] = []
    for p in psutil.process_iter(attrs=attrs):
        try:
            info = p.info
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

        mem_info = info.get("memory_info")
        item: dict[str, Any] = {
            "pid": info.get("pid"),
            "name": info.get("name") or "",
            "username": info.get("username"),
            "cpu_percent": round(info.get("cpu_percent") or 0.0, 1),
            "memory_percent": round(info.get("memory_percent") or 0.0, 1),
            "memory_rss": mem_info.rss if mem_info else 0,
            "create_time": round(info.get("create_time") or 0.0, 1),
            "exe": info.get("exe"),
            "cmdline": " ".join(info.get("cmdline") or []) or "",
            "status": info.get("status"),
        }
        if key == "disk":
            disk_read = disk_write = 0
            try:
                io = p.io_counters()
                disk_read = io.read_bytes
                disk_write = io.write_bytes
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
            item["disk_read_bytes"] = disk_read
            item["disk_write_bytes"] = disk_write
        processes.append(item)

    if key == "memory":
        processes.sort(key=lambda d: d["memory_percent"], reverse=True)
    elif key == "name":
        processes.sort(key=lambda d: (d["name"] or "").lower())
    elif key == "disk":
        processes.sort(
            key=lambda d: (d.get("disk_read_bytes", 0) + d.get("disk_write_bytes", 0)),
            reverse=True,
        )
    else:  # cpu
        processes.sort(key=lambda d: d["cpu_percent"], reverse=True)

    return processes[:200]


def kill_process(pid: int) -> dict[str, Any]:
    """Terminate a process by PID, escalating to ``kill()`` if needed."""
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return {"ok": False, "message": f"Process {pid} not found"}

    try:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except psutil.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
    except psutil.NoSuchProcess:
        return {"ok": True, "message": f"Process {pid} already exited"}
    except psutil.AccessDenied:
        return {"ok": False, "message": f"Access denied terminating process {pid}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"Failed to terminate process {pid}: {exc}"}

    return {"ok": True, "message": f"Process {pid} terminated"}


def reveal_process(pid: int) -> dict[str, Any]:
    """Open Explorer with the process's executable selected.

    Resolves the process executable via psutil and runs
    ``explorer /select,"<exe>"`` so the file is highlighted in its folder.
    Windows-only; returns ``{ok, path}``.
    """
    if os.name != "nt":
        return {"ok": False, "message": "文件定位仅支持 Windows"}

    try:
        exe = psutil.Process(pid).exe()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return {"ok": False, "message": f"无法访问进程 {pid} 或其可执行文件路径"}

    if not exe:
        return {"ok": False, "message": f"进程 {pid} 没有可执行文件路径"}

    try:
        subprocess.Popen(["explorer", f'/select,"{exe}"'])
    except OSError as exc:
        return {"ok": False, "message": f"打开资源管理器失败: {exc}"}

    return {"ok": True, "path": exe}


def list_power_plans() -> list[dict[str, Any]]:
    """List Windows power plans (GUID + display name + active flag).

    Returns ``[]`` on non-Windows or if ``powercfg`` is unavailable. Results are cached
    for a few seconds because ``powercfg /list`` is a subprocess that can be slow on a
    busy system, and the plan list rarely changes between UI polls.
    """
    if os.name != "nt":
        return []

    now = time.time()
    if now - _power_plan_cache["ts"] < _POWER_PLAN_CACHE_TTL:
        return list(_power_plan_cache["plans"])

    try:
        proc = subprocess.run(
            ["powercfg", "/list"],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []

    if proc.returncode != 0:
        return []

    plans: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        guid_match = _GUID_RE.search(line)
        if not guid_match:
            continue
        guid = guid_match.group(0).lower()
        name_match = _NAME_RE.search(line)
        name = name_match.group(1).strip() if name_match else guid
        active = line.rstrip().endswith("*")
        plans.append({"name": name, "guid": guid, "active": active})

    _power_plan_cache["ts"] = time.time()
    _power_plan_cache["plans"] = list(plans)
    return plans


def set_power_plan(name: str) -> dict[str, Any]:
    """Activate a Windows power plan by friendly name or GUID."""
    if os.name != "nt":
        return {"ok": False, "message": "Power plan switching is Windows-only"}

    guid = _resolve_power_plan_guid(name)
    if guid is None:
        return {"ok": False, "message": f"Unknown power plan: {name}"}

    try:
        proc = subprocess.run(
            ["powercfg", "/setactive", guid],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return {"ok": False, "message": f"powercfg failed: {exc}"}

    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        return {"ok": False, "message": detail or f"powercfg exited with code {proc.returncode}"}

    return {"ok": True, "message": f"Power plan set to {name}"}


def _resolve_power_plan_guid(name: str) -> str | None:
    """Resolve a friendly name (Chinese or English) to a power-plan GUID."""
    stripped = name.strip()
    if not stripped:
        return None

    if stripped in _POWER_PLAN_GUIDS:
        return _POWER_PLAN_GUIDS[stripped]

    key = stripped.lower()
    for plan in list_power_plans():
        if plan["name"].strip().lower() == key:
            return plan["guid"]

    if _GUID_RE.fullmatch(stripped):
        return stripped.lower()

    return None


# ---------------------------------------------------------------------------
# Startup items (Windows registry Run keys + shell:startup folders)
# ---------------------------------------------------------------------------

try:
    import winreg
except ImportError:  # pragma: no cover - non-Windows
    winreg = None  # type: ignore[assignment]

_STARTUP_RUN_KEYS: list[tuple[Any, str, str]] = []
if winreg is not None:
    _STARTUP_RUN_KEYS = [
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", "HKCU"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run", "HKLM"),
    ]


def _load_disabled_startup_items() -> dict[str, dict[str, Any]]:
    """Load the disabled startup items map (name -> {command, location, source})."""
    raw = settings.get("disabled_startup_items", "{}")
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_disabled_startup_items(data: dict[str, dict[str, Any]]) -> None:
    settings.set("disabled_startup_items", json.dumps(data, ensure_ascii=False))


def list_startup_items() -> list[dict[str, Any]]:
    """Enumerate startup items from registry Run keys and shell:startup folders.

    Items that were disabled (deleted from the registry) are re-added from the
    ``disabled_startup_items`` settings map with ``enabled=False`` so they remain
    visible and can be re-enabled.
    """
    if os.name != "nt":
        return []

    disabled = _load_disabled_startup_items()
    items: list[dict[str, Any]] = []
    items.extend(_startup_items_from_registry())
    items.extend(_startup_items_from_folders())

    seen: set[str] = set()
    for item in items:
        item["enabled"] = item["name"] not in disabled
        seen.add(item["name"])

    for name, record in disabled.items():
        if name in seen:
            continue
        items.append(
            {
                "name": name,
                "command": record.get("command", ""),
                "location": record.get("location", ""),
                "source": record.get("source", "注册表"),
                "enabled": False,
            }
        )

    return items


def _startup_items_from_registry() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for hive, path, location in _STARTUP_RUN_KEYS:
        try:
            key = winreg.OpenKey(hive, path)
        except OSError:
            continue
        with key:
            index = 0
            while True:
                try:
                    name, command, _ = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                items.append(
                    {
                        "name": name or "",
                        "command": command or "",
                        "location": location,
                        "source": "注册表",
                        "enabled": True,
                    }
                )
    return items


def _startup_items_from_folders() -> list[dict[str, Any]]:
    """List entries in the per-user and all-users shell:startup folders."""
    folders = [
        (os.environ.get("APPDATA", ""), "HKCU"),
        (os.environ.get("ProgramData", ""), "HKLM"),
    ]
    items: list[dict[str, Any]] = []
    for base, location in folders:
        if not base:
            continue
        startup_dir = os.path.join(base, r"Microsoft\Windows\Start Menu\Programs\Startup")
        try:
            with os.scandir(startup_dir) as entries:
                for entry in entries:
                    try:
                        if entry.name.startswith(".") or entry.is_dir():
                            continue
                        items.append(
                            {
                                "name": entry.name,
                                "command": entry.path,
                                "location": location,
                                "source": "启动文件夹",
                                "enabled": True,
                            }
                        )
                    except OSError:
                        continue
        except OSError:
            continue
    return items


def set_startup_item(name: str, enabled: bool) -> dict[str, Any]:
    """Enable/disable a startup item by deleting/restoring its Run registry value.

    Disabling deletes the registry ``Run`` value and stores its command (plus
    location) under the ``disabled_startup_items`` settings key; enabling restores
    it. Only registry ``Run`` entries are togglable — startup-folder entries are
    left untouched and reported as such.
    """
    if os.name != "nt":
        return {"ok": False, "message": "开机启动项管理仅支持 Windows"}

    disabled = _load_disabled_startup_items()

    if not enabled:
        entry = _find_run_value(name)
        if entry is None:
            return {"ok": False, "message": f"未找到注册表启动项: {name}"}
        hive, path, location, command, value_type = entry
        try:
            key = winreg.OpenKey(hive, path, 0, winreg.KEY_SET_VALUE)
        except OSError as exc:
            return {"ok": False, "message": f"无法打开注册表项: {exc}"}
        try:
            with key:
                winreg.DeleteValue(key, name)
        except OSError as exc:
            return {"ok": False, "message": f"删除启动项失败: {exc}"}
        disabled[name] = {"command": command, "location": location, "source": "注册表", "type": int(value_type)}
        _save_disabled_startup_items(disabled)
        return {"ok": True, "message": f"已禁用启动项 {name}"}

    if name in disabled:
        record = disabled[name]
        loc = _reg_location(record.get("location"))
        if loc is None:
            return {"ok": False, "message": f"无法确定启动项位置: {name}"}
        hive, path = loc
        try:
            key = winreg.OpenKey(hive, path, 0, winreg.KEY_SET_VALUE)
        except OSError as exc:
            return {"ok": False, "message": f"无法打开注册表项: {exc}"}
        try:
            with key:
                winreg.SetValueEx(
                    key, name, 0, _restore_value_type(record.get("type")), record.get("command", "")
                )
        except OSError as exc:
            return {"ok": False, "message": f"恢复启动项失败: {exc}"}
        del disabled[name]
        _save_disabled_startup_items(disabled)
        return {"ok": True, "message": f"已启用启动项 {name}"}

    return {"ok": True, "message": f"启动项 {name} 已启用"}


def _find_run_value(name: str) -> tuple[Any, str, str, str, int] | None:
    """Return ``(hive, path, location, command, value_type)`` for a Run value by name."""
    for hive, path, location in _STARTUP_RUN_KEYS:
        try:
            key = winreg.OpenKey(hive, path)
        except OSError:
            continue
        with key:
            try:
                command, value_type = winreg.QueryValueEx(key, name)
            except OSError:
                continue
            return (hive, path, location, command or "", value_type)
    return None


def _restore_value_type(type_value: Any) -> int:
    """Map a stored registry value type back to a ``winreg`` type constant.

    ``REG_EXPAND_SZ`` values (e.g. ``%SystemRoot%\\...``) must be restored with their
    original type or Windows will not re-expand them; legacy records saved without a
    type default to ``REG_SZ``.
    """
    try:
        t = int(type_value)
    except (TypeError, ValueError):
        return winreg.REG_SZ
    return t if t in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) else winreg.REG_SZ


def _reg_location(location: Any) -> tuple[Any, str] | None:
    """Map a stored location label (HKCU/HKLM) back to a ``(hive, path)`` tuple."""
    for hive, path, loc in _STARTUP_RUN_KEYS:
        if loc == location:
            return (hive, path)
    return None


async def _monitor_loop() -> None:
    """Periodically publish system stats and throttled alerts."""
    last_alert = 0.0
    while True:
        try:
            stats = await asyncio.to_thread(get_system_stats)
            publish("system_stats", stats)

            cpu = float(stats.get("cpu_percent") or 0.0)
            disk_high = any((d.get("percent") or 0.0) > 90.0 for d in stats.get("disk", []))
            gpu_threshold = settings.get_float("gpu_temp_threshold", 85.0)
            gpu_hot = any(
                (g.get("temperature") or 0.0) > gpu_threshold for g in stats.get("gpu", [])
            )
            now = time.time()
            if (cpu > 90.0 or disk_high or gpu_hot) and (now - last_alert >= 60.0):
                last_alert = now
                if cpu > 90.0:
                    title = "CPU 使用率过高"
                    message = f"CPU 使用率已达到 {cpu:.1f}%"
                elif disk_high:
                    title = "磁盘空间不足"
                    message = "有磁盘分区使用率超过 90%"
                else:
                    title = "GPU 温度过高"
                    message = f"有 GPU 温度超过 {gpu_threshold:.0f}°C"
                publish("system_alert", {"title": title, "message": message})
        except Exception:  # noqa: BLE001
            logger.exception("system monitor tick failed")

        await asyncio.sleep(2)


def start() -> None:
    """Start the background monitor loop (idempotent, non-blocking)."""
    global _started
    if _started:
        return
    _started = True

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Called outside an event loop — run the loop on a daemon thread.
        threading.Thread(target=lambda: asyncio.run(_monitor_loop()), daemon=True).start()
        return

    loop.create_task(_monitor_loop())

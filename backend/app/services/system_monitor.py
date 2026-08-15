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
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Any

import psutil

from app.ws import publish

logger = logging.getLogger(__name__)

#: Module-level network-rate state (previous counters + timestamp).
_net_lock = threading.Lock()
_prev_net: dict[str, float] = {"bytes_sent": 0.0, "bytes_recv": 0.0, "timestamp": 0.0}

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
        "gpu": gpu,
        "uptime_seconds": uptime_seconds,
    }


def _gpu_stats() -> list[dict[str, Any]]:
    """Read GPU metrics via ``nvidia-smi``, falling back to GPUtil, else ``[]``."""
    result = _gpu_stats_nvidia_smi()
    if result:
        return result
    return _gpu_stats_gputil()


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
            timeout=10,
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

    ``sort_by`` may be ``cpu``, ``memory`` (both descending) or ``name``
    (ascending, case-insensitive).
    """
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
        processes.append(
            {
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
        )

    key = sort_by.strip().lower() if sort_by else "cpu"
    if key == "memory":
        processes.sort(key=lambda d: d["memory_percent"], reverse=True)
    elif key == "name":
        processes.sort(key=lambda d: (d["name"] or "").lower())
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


def list_power_plans() -> list[dict[str, Any]]:
    """List Windows power plans (GUID + display name + active flag).

    Returns ``[]`` on non-Windows or if ``powercfg`` is unavailable.
    """
    if os.name != "nt":
        return []

    try:
        proc = subprocess.run(
            ["powercfg", "/list"],
            capture_output=True,
            text=True,
            timeout=15,
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


async def _monitor_loop() -> None:
    """Periodically publish system stats and throttled alerts."""
    last_alert = 0.0
    while True:
        try:
            stats = await asyncio.to_thread(get_system_stats)
            publish("system_stats", stats)

            cpu = float(stats.get("cpu_percent") or 0.0)
            disk_high = any((d.get("percent") or 0.0) > 90.0 for d in stats.get("disk", []))
            now = time.time()
            if (cpu > 90.0 or disk_high) and (now - last_alert >= 60.0):
                last_alert = now
                if cpu > 90.0:
                    title = "CPU 使用率过高"
                    message = f"CPU 使用率已达到 {cpu:.1f}%"
                else:
                    title = "磁盘空间不足"
                    message = "有磁盘分区使用率超过 90%"
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

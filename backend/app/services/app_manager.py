"""Application management service.

Discovers installed applications (Start Menu / Desktop ``.lnk`` shortcuts and
Windows registry ``Uninstall`` entries), caches them in the ``apps`` table, and
provides launch / uninstall / favorite / icon helpers.

Windows-only APIs (``winreg``, ``pylnk3``, ``pywin32``) are imported lazily and
every function degrades to an empty result when they are unavailable. All
functions here are synchronous and are meant to be called from FastAPI's
threadpool (sync route handlers) or wrapped with ``asyncio.to_thread``.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from app import db, ws
from app.config import DATA_DIR

_UNINSTALL_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
_WOW64_UNINSTALL_SUBKEY = (
    r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"
)


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def _start_menu_dirs() -> list[Path]:
    """Return the Start Menu + Desktop directories to scan for ``.lnk`` files."""
    dirs: list[Path] = []
    for env, *tail in (
        ("APPDATA", "Microsoft", "Windows", "Start Menu", "Programs"),
        ("ProgramData", "Microsoft", "Windows", "Start Menu", "Programs"),
        ("USERPROFILE", "Desktop"),
        ("PUBLIC", "Desktop"),
    ):
        base = os.environ.get(env)
        if base:
            dirs.append(Path(base).joinpath(*tail))
    return dirs


def _iter_lnk_files() -> list[Path]:
    """Enumerate every ``.lnk`` shortcut under the Start Menu and Desktops."""
    files: list[Path] = []
    for base in _start_menu_dirs():
        try:
            files.extend(base.rglob("*.lnk"))
        except OSError:
            continue
    return files


def _resolve_lnk_target(lnk_path: Path) -> str | None:
    """Resolve a ``.lnk`` target path (pylnk3 first, binary heuristic fallback)."""
    try:
        import pylnk3
    except ImportError:
        pass
    else:
        try:
            lnk = pylnk3.for_file(str(lnk_path))
            target = getattr(lnk, "path", None)
            if target:
                return str(target)
        except Exception:
            pass
    return _extract_lnk_target_fallback(lnk_path)


def _extract_lnk_target_fallback(lnk_path: Path) -> str | None:
    """Best-effort: pull an ``X:\\...`` path string out of the raw ``.lnk`` bytes."""
    try:
        raw = lnk_path.read_bytes()
    except OSError:
        return None
    pattern = re.compile(r"[A-Za-z]:\\[^\x00\r\n\t]{2,200}")
    for encoding in ("latin-1", "utf-16-le"):
        try:
            text = raw.decode(encoding, errors="ignore")
        except Exception:
            continue
        match = pattern.search(text)
        if match:
            candidate = match.group(0).rstrip(" \\")
            if candidate:
                return candidate
    return None


def _clean_path(value: Any) -> str:
    """Strip surrounding quotes / whitespace from a registry string value."""
    return str(value or "").strip().strip('"').strip()


def _exe_from_command(command: str) -> str:
    """Extract a bare executable path from an UninstallString command line."""
    command = (command or "").strip()
    if not command:
        return ""
    if command.startswith('"'):
        end = command.find('"', 1)
        if end != -1:
            return _clean_path(command[: end + 1])
        return _clean_path(command)
    if re.match(r"^[A-Za-z]:\\", command):
        return _clean_path(command.split(" ", 1)[0])
    return ""


def _enum_registry_uninstall() -> list[dict[str, str]]:
    """Enumerate HKLM/HKCU Uninstall keys.

    Returns a list of ``{"name", "path", "uninstall_string"}`` entries. On
    non-Windows systems (no ``winreg``) this returns an empty list.
    """
    try:
        import winreg
    except ImportError:
        return []

    roots = (
        (winreg.HKEY_LOCAL_MACHINE, _UNINSTALL_SUBKEY),
        (winreg.HKEY_LOCAL_MACHINE, _WOW64_UNINSTALL_SUBKEY),
        (winreg.HKEY_CURRENT_USER, _UNINSTALL_SUBKEY),
    )
    entries: list[dict[str, str]] = []
    for root, sub in roots:
        try:
            key = winreg.OpenKey(root, sub)
        except OSError:
            continue
        try:
            count = winreg.QueryInfoKey(key)[0]
            for i in range(count):
                try:
                    sub_name = winreg.EnumKey(key, i)
                except OSError:
                    continue
                try:
                    sub_key = winreg.OpenKey(key, sub_name)
                except OSError:
                    continue
                try:
                    def _get(value_name: str) -> Any:
                        try:
                            value, _ = winreg.QueryValueEx(sub_key, value_name)
                            return value
                        except OSError:
                            return ""

                    display_name = str(_get("DisplayName") or "").strip()
                    if not display_name:
                        continue
                    install_location = _clean_path(_get("InstallLocation"))
                    display_icon = _clean_path(str(_get("DisplayIcon")).split(",", 1)[0])
                    uninstall_string = str(_get("UninstallString") or "").strip()
                    target = (
                        install_location
                        or display_icon
                        or _exe_from_command(uninstall_string)
                    )
                    entries.append(
                        {
                            "name": display_name,
                            "path": target,
                            "uninstall_string": uninstall_string,
                        }
                    )
                finally:
                    winreg.CloseKey(sub_key)
        finally:
            winreg.CloseKey(key)
    return entries


def _find_uninstall_entry(name: str) -> dict[str, str] | None:
    """Return the registry uninstall entry whose DisplayName matches ``name``."""
    needle = name.strip().lower()
    for entry in _enum_registry_uninstall():
        if entry["name"].strip().lower() == needle:
            return entry
    return None


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def _upsert_app(name: str, path: str) -> bool:
    """Insert an app if not already present; return True when a new row is added."""
    name = name.strip()
    path = path.strip()
    if not name or not path:
        return False
    if db.query_one("SELECT id FROM apps WHERE name = ? AND path = ?", (name, path)):
        return False
    db.execute(
        "INSERT INTO apps(name, path) VALUES (?, ?) ON CONFLICT(name, path) DO NOTHING",
        (name, path),
    )
    return (
        db.query_one("SELECT id FROM apps WHERE name = ? AND path = ?", (name, path))
        is not None
    )


def scan_apps() -> dict[str, int]:
    """Scan shortcuts + registry uninstall entries and cache them in ``apps``."""
    added = 0
    total = 0
    for lnk in _iter_lnk_files():
        name = lnk.stem.strip()
        if not name:
            continue
        target = _resolve_lnk_target(lnk) or str(lnk)
        total += 1
        if _upsert_app(name, target):
            added += 1
    for entry in _enum_registry_uninstall():
        total += 1
        if _upsert_app(entry["name"], entry["path"]):
            added += 1
    ws.publish("apps_scanned", {"added": added, "total": total})
    db.log_operation("apps_scan", {}, {"added": added, "total": total})
    return {"added": added, "total": total}


def list_apps(favorites_only: bool = False) -> list[dict[str, Any]]:
    """Return all cached applications (optionally only favorites)."""
    sql = "SELECT * FROM apps"
    params: tuple = ()
    if favorites_only:
        sql += " WHERE is_favorite = 1"
    sql += " ORDER BY is_favorite DESC, name ASC"
    rows = db.query(sql, params)
    for row in rows:
        row["is_favorite"] = bool(row["is_favorite"])
    return rows


def recommend_apps(limit: int = 8) -> list[dict[str, Any]]:
    """Return the apps the user has launched, most-used first.

    Only apps with a positive ``launch_count`` or a recorded ``last_launched``
    timestamp are considered, ordered by usage frequency then recency.
    """
    rows = db.query(
        "SELECT * FROM apps "
        "WHERE launch_count > 0 OR last_launched > 0 "
        "ORDER BY launch_count DESC, last_launched DESC LIMIT ?",
        (limit,),
    )
    for row in rows:
        row["is_favorite"] = bool(row["is_favorite"])
    return rows


def set_favorite(app_id: int, is_favorite: bool) -> dict[str, Any]:
    """Mark/unmark an application as a favorite."""
    db.execute(
        "UPDATE apps SET is_favorite = ? WHERE id = ?",
        (1 if is_favorite else 0, app_id),
    )
    db.log_operation(
        "app_favorite", {"id": app_id, "is_favorite": is_favorite}, {"ok": True}
    )
    return {"ok": True, "id": app_id, "is_favorite": bool(is_favorite)}


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #
_SCRIPT_EXTS = {".py", ".bat", ".cmd", ".ps1", ".sh"}


def _is_script(path: str) -> bool:
    """Return True when ``path`` points to a script we can route through Claude Code."""
    return Path(path).suffix.lower() in _SCRIPT_EXTS


def _script_run_command(path: str) -> str:
    """Build the shell command that executes the given script path."""
    ext = Path(path).suffix.lower()
    quoted = f'"{path}"'
    if ext == ".py":
        return f"python {quoted}"
    if ext == ".ps1":
        return f"powershell -NoProfile -ExecutionPolicy Bypass -File {quoted}"
    if ext == ".sh":
        return f"bash {quoted}"
    return quoted  # .bat / .cmd run directly


def _run_script_via_claude(path: str) -> bool:
    """Ask Claude Code to run ``path`` one-shot via ``claude -p "<run command>"``.

    Returns True when the one-shot CLI was spawned, False when the Claude CLI is
    missing or the spawn failed (caller falls back to the native launcher).
    """
    from app.services import claude_code

    claude = claude_code._resolve_claude()
    if not claude:
        return False
    argv = [claude, "-p", _script_run_command(path)]
    if os.name == "nt" and claude.lower().endswith((".cmd", ".bat")):
        argv = ["cmd", "/c", " ".join(claude_code._cmd_escape_arg(a) for a in argv)]
    try:
        subprocess.Popen(argv)
    except OSError:
        return False
    return True


def start_app(app_id: int) -> dict[str, Any]:
    """Launch the application at ``apps.path`` and update usage counters.

    Script targets (``.py``/``.bat``/``.cmd``/``.ps1``/``.sh``) are handed to
    Claude Code (``claude -p "<run command>"``) instead of the native launcher;
    everything else uses ``os.startfile`` (Windows) or ``subprocess.Popen``.
    """
    row = db.query_one("SELECT * FROM apps WHERE id = ?", (app_id,))
    if not row:
        return {"ok": False, "error": f"app {app_id} not found"}
    path = str(row["path"] or "")
    if not path or not os.path.exists(path):
        result: dict[str, Any] = {"ok": False, "error": f"target not found: {path}"}
        db.log_operation("app_start", {"id": app_id, "name": row["name"]}, result)
        return result

    launch_method = "native"
    if _is_script(path) and _run_script_via_claude(path):
        launch_method = "claude"
    if launch_method != "claude":
        if os.name == "nt" and hasattr(os, "startfile"):
            os.startfile(path)
        else:
            subprocess.Popen([path])

    now = time.time()
    db.execute(
        "UPDATE apps SET launch_count = launch_count + 1, last_launched = ? WHERE id = ?",
        (now, app_id),
    )
    result = {
        "ok": True,
        "id": app_id,
        "name": row["name"],
        "path": path,
        "launch_method": launch_method,
    }
    db.log_operation(
        "app_start",
        {"id": app_id, "name": row["name"], "path": path, "launch_method": launch_method},
        result,
    )
    return result


def uninstall_app(app_id: int, confirmation_id: str | None = None) -> dict[str, Any]:
    """Run the registry UninstallString for the app (GUI installer, not silent).

    ``confirmation_id`` is expected to have been validated by the caller
    (router ``require_confirmation`` or the tool dispatcher); it is only logged
    here for the audit trail.
    """
    row = db.query_one("SELECT * FROM apps WHERE id = ?", (app_id,))
    if not row:
        return {"ok": False, "error": f"app {app_id} not found"}
    name = str(row["name"])
    entry = _find_uninstall_entry(name)
    if entry is None:
        result = {"ok": False, "error": f"no uninstall entry found for {name!r}"}
        db.log_operation(
            "app_uninstall", {"id": app_id, "name": name, "confirmation_id": confirmation_id}, result
        )
        return result
    command = (entry.get("uninstall_string") or "").strip()
    if not command:
        result = {"ok": False, "error": "uninstall entry has no UninstallString"}
        db.log_operation(
            "app_uninstall", {"id": app_id, "name": name, "confirmation_id": confirmation_id}, result
        )
        return result
    # Spawn the native (GUI) uninstaller and return immediately.
    subprocess.Popen(command, shell=True)
    result = {"ok": True, "id": app_id, "name": name, "command": command}
    db.log_operation(
        "app_uninstall",
        {"id": app_id, "name": name, "command": command, "confirmation_id": confirmation_id},
        result,
    )
    return result


# --------------------------------------------------------------------------- #
# Icons
# --------------------------------------------------------------------------- #
def _extract_icon_powershell(path: str, out_path: Path) -> bool:
    """Render the associated icon to ``out_path`` as PNG via System.Drawing."""
    ps_path = path.replace("'", "''")
    ps_out = str(out_path).replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Drawing;"
        f"$i=[System.Drawing.Icon]::ExtractAssociatedIcon('{ps_path}');"
        "if($null -eq $i){exit 1};"
        "$b=$i.ToBitmap();"
        f"$b.Save('{ps_out}',[System.Drawing.Imaging.ImageFormat]::Png);"
        "$b.Dispose();$i.Dispose()"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0


def extract_icon(path: str, app_id: int) -> str | None:
    """Extract the associated icon from ``path`` and cache it as ``data/icons/<id>.png``.

    Uses ``System.Drawing`` (via PowerShell) on Windows, which reliably renders
    the associated icon to a 32-bit PNG. Returns the icon path on success and
    ``None`` on any failure. The ``apps.icon_path`` column is updated so the icon
    is served on subsequent ``GET /apps/icon/{id}`` calls.
    """
    if os.name != "nt":
        return None
    if not path or not os.path.exists(path):
        return None

    icon_dir = DATA_DIR / "icons"
    try:
        icon_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    out_path = icon_dir / f"{app_id}.png"

    if not _extract_icon_powershell(path, out_path):
        return None

    db.execute("UPDATE apps SET icon_path = ? WHERE id = ?", (str(out_path), app_id))
    return str(out_path)

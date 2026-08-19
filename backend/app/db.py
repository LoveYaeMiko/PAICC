"""SQLite persistence layer.

Thread-local connections + a module-level write lock make this safe to call from
FastAPI request handlers (threadpool) and background threads alike.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from app.config import DATA_DIR

DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(DATA_DIR) / "paicc.db"

_local = threading.local()
_write_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS apps (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    path          TEXT NOT NULL,
    icon_path     TEXT DEFAULT '',
    is_favorite   INTEGER DEFAULT 0,
    launch_count  INTEGER DEFAULT 0,
    last_launched REAL DEFAULT 0,
    UNIQUE(name, path)
);

CREATE TABLE IF NOT EXISTS operation_logs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    user      TEXT NOT NULL DEFAULT 'local',
    action    TEXT NOT NULL,
    params    TEXT DEFAULT '{}',
    result    TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS quant_projects (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    root_path        TEXT NOT NULL,
    config_file      TEXT DEFAULT '',
    dashboard_script TEXT DEFAULT '',
    log_dir          TEXT DEFAULT 'logs',
    is_active        INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS quant_commands (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL,
    name        TEXT NOT NULL,
    command     TEXT NOT NULL,
    description TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS quant_redline_history (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     REAL NOT NULL,
    source TEXT DEFAULT '',
    name   TEXT NOT NULL,
    label  TEXT DEFAULT '',
    level  TEXT NOT NULL,
    value  TEXT,
    detail TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS research_documents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL,
    file_path    TEXT NOT NULL,
    ingested_at  REAL NOT NULL,
    chunk_count  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS reports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_at  REAL NOT NULL,
    content       TEXT NOT NULL,
    sent_to_email INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS conversations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS papers (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    arxiv_id       TEXT UNIQUE,
    title          TEXT NOT NULL,
    authors        TEXT DEFAULT '',
    abstract       TEXT DEFAULT '',
    categories     TEXT DEFAULT '',
    fields         TEXT DEFAULT '',
    published_at   TEXT DEFAULT '',
    url            TEXT DEFAULT '',
    source         TEXT DEFAULT '',
    citation_count INTEGER DEFAULT 0,
    venue          TEXT DEFAULT '',
    score          REAL DEFAULT 0,
    set_tag        TEXT DEFAULT '',
    crawl_date     TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS paper_reports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date   TEXT NOT NULL,
    report_type   TEXT NOT NULL,
    content       TEXT NOT NULL,
    file_path     TEXT DEFAULT '',
    sent_to_email INTEGER DEFAULT 0
);
"""


def get_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        _local.conn = conn
    return _local.conn


def init_db() -> None:
    conn = get_conn()
    with _write_lock:
        conn.executescript(SCHEMA)
        conn.commit()
    _seed_defaults()


def _seed_defaults() -> None:
    from app.config import settings

    # Seed the default quant project if it doesn't exist.
    if not query("SELECT id FROM quant_projects LIMIT 1"):
        root = settings.get_quant_root()
        execute(
            "INSERT INTO quant_projects(name, root_path, config_file, log_dir, is_active) "
            "VALUES (?, ?, ?, ?, 1)",
            ("FQA", root, settings.get("quant_config_file"), settings.get("quant_log_dir")),
        )


def query(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    cur = get_conn().execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def query_one(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple = ()) -> int:
    conn = get_conn()
    with _write_lock:
        cur = conn.execute(sql, params)
        conn.commit()
    return cur.lastrowid


def get_setting(key: str, default: Any = None) -> Any:
    row = query_one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: Any) -> None:
    execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def delete_setting(key: str) -> None:
    execute("DELETE FROM settings WHERE key = ?", (key,))


def log_operation(action: str, params: Any = None, result: Any = None, user: str = "local") -> int:
    return execute(
        "INSERT INTO operation_logs(timestamp, user, action, params, result) VALUES (?, ?, ?, ?, ?)",
        (
            time.time(),
            user,
            action,
            json.dumps(params or {}, ensure_ascii=False, default=str),
            json.dumps(result or {}, ensure_ascii=False, default=str),
        ),
    )


def list_operations(limit: int = 200) -> list[dict[str, Any]]:
    rows = query("SELECT * FROM operation_logs ORDER BY id DESC LIMIT ?", (limit,))
    for r in rows:
        try:
            r["params"] = json.loads(r["params"])
        except Exception:
            pass
        try:
            r["result"] = json.loads(r["result"])
        except Exception:
            pass
    return rows

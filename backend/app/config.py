"""Runtime configuration.

Resolution order (highest first):
  1. SQLite ``settings`` table — an explicit, non-empty user override. This is what
     makes Settings-UI edits persist across restarts. Values equal to the built-in
     default, or equal to an environment variable, are *not* stored here (so secrets
     stay in ``backend/.env`` and a future ``.env`` edit keeps taking effect).
  2. Environment variables (``PAICC_<KEY_UPPER>``) — including values loaded from
     ``backend/.env``. These act as the fallback configuration (secrets such as the
     LLM API key or SMTP password live here so they stay out of git).
  3. Built-in defaults below.

The DB layer is imported lazily so this module never creates a circular import and
the settings can be read before the database is initialised.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# backend/ root
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
DATA_DIR = Path(os.getenv("PAICC_DATA_DIR", str(BASE_DIR / "data")))
LOGS_DIR = Path(os.getenv("PAICC_LOGS_DIR", str(BASE_DIR / "logs")))
CHROMA_DIR = Path(os.getenv("PAICC_CHROMA_DIR", str(DATA_DIR / "chroma")))

# FQA is the sibling quant repo — ``Desktop/FQA`` next to ``Desktop/PAICC``. The
# default is derived from PAICC's own location so a checkout on any machine points
# at the right sibling instead of a hardcoded developer path (overridable via the
# Settings UI or the ``PAICC_QUANT_ROOT`` env var).
_QUANT_ROOT_DEFAULT = str(BASE_DIR.parent / "FQA")

DEFAULTS: dict[str, Any] = {
    # Quant project
    "quant_root": _QUANT_ROOT_DEFAULT,
    "quant_config_file": "configs/master_config.yaml",
    "quant_log_dir": "logs",
    "quant_dashboard_script": "",
    # Quant shadow/calibration scheduler (weekday EOD + weekly calibration).
    # The daily closed loop runs at 15:10 — right after the 15:00 closing
    # auction (the D track's 14:55 preclose orders fill at the auction close),
    # instead of the legacy 17:30 evening slot.
    "quant_shadow_daily_time": "15:10",
    "quant_calibrate_time": "18:00",
    "quant_shadow_auto_email": "true",
    # Drive the daily job with FQA's end-to-end autopilot closed loop
    # (shadow → kill-switch risk gate → periodic §7 re-calibration → factor-decay
    # monitor) instead of a bare ``shadow`` run. Disable to fall back to shadow-only.
    "quant_autopilot_enabled": "true",
    # D-track model self-optimization cycle (monthly rolling refit + parallel
    # challenger + forward promotion gate, replacing the old Saturday calibrate
    # and Sunday weekly). OFF by default — enabled only after the historical
    # evidence experiment (scripts/d_model_cycle_eval.py) shows a positive
    # out-of-sample effect.
    "quant_d_cycle_enabled": "false",
    # LLM
    "llm_provider": "deepseek",
    "llm_model": "deepseek-chat",
    "llm_base_url": "https://api.deepseek.com",
    "llm_api_key": "",
    "llm_temperature": "0.7",
    # Financial-expert commentary appended to the daily shadow report email.
    # Uses a dedicated model/API key so it can run a higher-tier model without
    # disturbing the main LLM settings.
    "quant_commentary_enabled": "true",
    "quant_commentary_model": "deepseek-v4-pro",
    "quant_commentary_api_key": "",
    # Knowledge / research
    "knowledge_threshold": "0.5",
    "search_api_provider": "",
    "search_api_key": "",
    "embedding_model": "BAAI/bge-small-zh-v1.5",
    # Email
    "smtp_host": "",
    "smtp_port": "465",
    "smtp_user": "",
    "smtp_password": "",
    "smtp_from": "",
    "smtp_to": "",
    # Storage report schedule
    "report_schedule": "weekly",
    # Paper recommendation / literature review
    "papers_dir": str(Path.home() / "Desktop" / "papers"),
    "paper_daily_time": "09:00",
    "paper_monthly_time": "09:30",
    "arxiv_categories": "cs.AI,cs.LG,cs.CL,cs.CV,cs.NE,cs.RO,stat.ML",
    "papers_proxy": "",
    "paper_s2_api_key": "",
    # Misc
    "backend_port": "8000",
    "everything_path": "es.exe",
    "claude_path": "",
    # Claude Code CLI (empty = inherit the user's own Claude Code config)
    "claude_permission_mode": "",
    "claude_model": "",
    "default_user": "local",
}


def _db() -> Any:
    """Lazily import the db module (avoids circular import)."""
    from app import db

    return db


class Settings:
    """Typed, layered access to configuration."""

    def get(self, key: str, default: Any = None) -> Any:
        default_val = DEFAULTS.get(key, default)
        try:
            val = _db().get_setting(key)
        except Exception:
            val = None

        # An explicit, non-empty override stored in the DB always wins. Non-overrides
        # are never written to the DB (see ``put_settings``), so a non-empty value here
        # is a genuine user edit, never a seeded default or an env value.
        if val is not None and str(val) != "":
            return val

        env_key = f"PAICC_{key.upper()}"
        if env_key in os.environ:
            return os.environ[env_key]

        return default_val

    def set(self, key: str, value: Any) -> None:
        try:
            _db().set_setting(key, str(value))
        except Exception:
            # DB not ready yet — silently ignore (values survive via env/defaults)
            pass

    def delete(self, key: str) -> None:
        try:
            _db().delete_setting(key)
        except Exception:
            pass

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.get(key, default))
        except (TypeError, ValueError):
            return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.get(key, default))
        except (TypeError, ValueError):
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        val = self.get(key, default)
        if isinstance(val, bool):
            return val
        return str(val).strip().lower() in ("1", "true", "yes", "on")

    def all(self) -> dict[str, Any]:
        """Return all keys merged with their effective values."""
        return {key: self.get(key) for key in DEFAULTS}

    def get_llm_config(self) -> dict[str, Any]:
        return {
            "provider": str(self.get("llm_provider", "deepseek")),
            "model": str(self.get("llm_model", "deepseek-chat")),
            "base_url": str(self.get("llm_base_url", "")),
            "api_key": str(self.get("llm_api_key", "")),
            "temperature": self.get_float("llm_temperature", 0.7),
        }

    def get_quant_root(self) -> str:
        return str(self.get("quant_root", ""))

    def get_papers_dir(self) -> str:
        return str(self.get("papers_dir", ""))


settings = Settings()

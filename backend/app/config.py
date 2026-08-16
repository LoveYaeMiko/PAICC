"""Runtime configuration.

Resolution order (highest first):
  1. Environment variables (``PAICC_<KEY_UPPER>``)
  2. SQLite ``settings`` table (editable from the Settings UI)
  3. Built-in defaults below

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

DEFAULTS: dict[str, Any] = {
    # Quant project
    "quant_root": r"C:\Users\wyxwi\Desktop\FQA",
    "quant_config_file": "configs/master_config.yaml",
    "quant_log_dir": "logs",
    "quant_dashboard_script": "",
    # LLM
    "llm_provider": "deepseek",
    "llm_model": "deepseek-chat",
    "llm_base_url": "https://api.deepseek.com",
    "llm_api_key": "",
    "llm_temperature": "0.7",
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
    # Misc
    "backend_port": "8000",
    "everything_path": "es.exe",
    "claude_path": "",
    "default_user": "local",
}


def _db() -> Any:
    """Lazily import the db module (avoids circular import)."""
    from app import db

    return db


class Settings:
    """Typed, layered access to configuration."""

    def get(self, key: str, default: Any = None) -> Any:
        env_key = f"PAICC_{key.upper()}"
        if env_key in os.environ:
            return os.environ[env_key]
        try:
            val = _db().get_setting(key)
            if val is not None:
                return val
        except Exception:
            pass
        return DEFAULTS.get(key, default)

    def set(self, key: str, value: Any) -> None:
        try:
            _db().set_setting(key, str(value))
        except Exception:
            # DB not ready yet — silently ignore (values survive via env/defaults)
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


settings = Settings()

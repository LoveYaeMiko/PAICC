"""Development entrypoint for the PAICC backend.

Run directly:
    python run.py
or:
    uvicorn app.main:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import os

import uvicorn

if __name__ == "__main__":
    port = int(os.getenv("PAICC_BACKEND_PORT", "8000"))
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=port,
        reload=False,
        log_level="info",
    )

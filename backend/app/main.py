"""PAICC FastAPI application entrypoint.

Wires together: SQLite init, the WebSocket event bus, background services
(system monitor / quant monitor / scheduler / claude scheduler), the Function-Calling
tool registry, and every router under ``/api``.
"""
from __future__ import annotations

import importlib
import logging
import sys
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from app import __version__, db, ws
from app.models.schemas import HealthResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("paicc")

START_TIME = time.time()

ROUTER_MODULES = [
    "meta",
    "system",
    "files",
    "apps",
    "storage",
    "quant",
    "research",
    "ai",
    "claude_code",
]

#: (module, start_function) — background services, started idempotently at boot.
BACKGROUND_SERVICES = [
    ("app.services.system_monitor", "start"),
    ("app.services.quant_manager", "start"),
    ("app.services.storage_analysis", "start_scheduler"),
    ("app.services.claude_code", "start"),
    ("app.services.se_knowledge", "seed"),
]


def _start_background_services() -> None:
    import app.tools  # noqa: F401  (registers all Function-Calling tools)

    for mod_name, fn_name in BACKGROUND_SERVICES:
        try:
            mod = importlib.import_module(mod_name)
            fn = getattr(mod, fn_name, None)
            if fn is not None:
                fn()
        except Exception:  # noqa: BLE001
            logger.warning("background service %s.%s skipped", mod_name, fn_name, exc_info=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    ws.manager.start()
    _start_background_services()
    logger.info("PAICC backend v%s ready", __version__)
    yield


app = FastAPI(
    title="Personal AI Command Center",
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # local desktop app; Electron renderer may use file:// or null origin
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        version=__version__,
        python_version=sys.version.split()[0],
        uptime_seconds=round(time.time() - START_TIME, 1),
    )


@app.websocket("/ws/events")
async def events_ws(websocket: WebSocket) -> None:
    await ws.manager.connect(websocket)
    try:
        while True:
            # Clients may send pings/acknowledgements; we mostly push server-side events.
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws.manager.disconnect(websocket)
    except Exception:  # noqa: BLE001
        ws.manager.disconnect(websocket)


for _name in ROUTER_MODULES:
    try:
        _mod = importlib.import_module(f"app.routers.{_name}")
        if hasattr(_mod, "router"):
            app.include_router(_mod.router, prefix="/api")
    except Exception:  # noqa: BLE001
        logger.warning("router app.routers.%s failed to load", _name, exc_info=True)

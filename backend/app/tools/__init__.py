"""AI Function-Calling tools.

Every module registers its tools in ``app/tools/<module>_tools.py`` using the ``@tool``
decorator from :mod:`app.tools.registry`. This package auto-discovers and imports every
``*_tools`` submodule so that importing ``app.tools`` registers everything.
"""
from __future__ import annotations

import importlib
import logging
import pkgutil

from app.tools import registry  # noqa: F401

logger = logging.getLogger(__name__)


def _register_all() -> None:
    import app.tools as pkg

    for mod in pkgutil.iter_modules(pkg.__path__):
        if mod.name.endswith("_tools"):
            try:
                importlib.import_module(f"{pkg.__name__}.{mod.name}")
            except Exception:  # noqa: BLE001
                logger.warning("tool module %s failed to load", mod.name, exc_info=True)


_register_all()

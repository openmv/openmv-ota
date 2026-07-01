"""Guard for the ``server`` optional-dependency extra.

The CLI's verb modules are imported on every ``openmv-ota`` invocation (argparse wiring), so they
must import cleanly on a *base* install. The heavy deps (fastapi/uvicorn/pydantic-settings/httpx)
are pulled in lazily inside handlers, *after* this guard turns a missing extra into a clear hint
rather than a raw ``ImportError``.
"""

from __future__ import annotations

import importlib
from typing import Callable

from .errors import ServerError

_HINT = ("the 'server' feature needs extra packages -- run: pip install openmv-ota[server]")


def require_server_extra(_import: Callable[[str], object] | None = None) -> None:
    """Raise ``ServerError`` (exit 2) unless the ``server`` extra is importable. ``_import`` is a
    test seam (defaults to ``importlib.import_module``)."""
    imp = _import or importlib.import_module
    try:
        imp("fastapi")
    except ImportError:
        raise ServerError(_HINT, exit_code=2) from None

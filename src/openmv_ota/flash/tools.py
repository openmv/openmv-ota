"""Locate the host flashing binaries.

The SDK bundles a known-good ``dfu-util`` (``<sdk_home>/bin/dfu-util``); prefer it when a
SDK home is known, fall back to one on ``PATH``, and let ``--dfu-util`` override outright.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .errors import FlashError


def find_dfu_util(override: str | None = None, sdk_home: Path | None = None) -> str:
    """Resolve the ``dfu-util`` to run: ``override`` > ``<sdk_home>/bin/dfu-util`` > PATH."""
    if override:
        return override
    if sdk_home is not None:
        cand = Path(sdk_home) / "bin" / "dfu-util"
        if cand.exists():
            return str(cand)
    found = shutil.which("dfu-util")
    if found:
        return found
    raise FlashError("dfu-util not found -- install it, put the SDK's on PATH, "
                     "or pass --dfu-util <path>")

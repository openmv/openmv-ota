"""Locate the host flashing binaries.

The SDK bundles a known-good ``dfu-util`` (``<sdk_home>/bin/dfu-util``) and the spsdk
``sdphost``/``blhost`` (``<sdk_home>/python/bin/``); prefer the SDK's when a home is known,
fall back to ``PATH``, and let an explicit override win outright.
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


def find_cubeprog(sdk_home: Path | None = None) -> str:
    """Resolve STM32CubeProgrammer's CLI (``<sdk_home>/stcubeprog/bin/STM32_Programmer_CLI``,
    else PATH) -- used to flash the N6 bootloader."""
    name = "STM32_Programmer_CLI"
    if sdk_home is not None:
        cand = Path(sdk_home) / "stcubeprog" / "bin" / name
        if cand.exists():
            return str(cand)
    found = shutil.which(name)
    if found:
        return found
    raise FlashError("%s not found -- it ships in the SDK's stcubeprog/bin; pass --sdk-home"
                     % name)


def find_spsdk(name: str, sdk_home: Path | None = None) -> str:
    """Resolve an spsdk tool (``sdphost``/``blhost``): ``<sdk_home>/python/bin/<name>`` > PATH."""
    if sdk_home is not None:
        cand = Path(sdk_home) / "python" / "bin" / name
        if cand.exists():
            return str(cand)
    found = shutil.which(name)
    if found:
        return found
    raise FlashError("%s not found -- it ships in the SDK's python/bin (spsdk); pass "
                     "--sdk-home or put it on PATH" % name)

"""Firmware-aware artifact builders.

`openmv-ota build romfs` compiles a project's app the way the pegged firmware
expects (``.py`` -> ``.mpy`` with the pegged mpy-cross, NPU models via the pegged
Vela / ST Edge AI) and packs a ROMFS image per target. It is OTA-agnostic: peg a
project, build a romfs, flash it.
"""

from __future__ import annotations

from .romfs import build_romfs

__all__ = ["build_romfs"]

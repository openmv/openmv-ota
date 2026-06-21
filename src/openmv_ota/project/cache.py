"""Where ``project setup`` clones the pinned firmware on this machine."""

from __future__ import annotations

import os
from pathlib import Path


def cache_root(override: str | None = None, os_name: str | None = None) -> Path:
    """The base directory for setup clones.

    Order: explicit ``override`` -> ``$OPENMV_OTA_CACHE`` -> a platform cache dir
    (``%LOCALAPPDATA%`` on Windows, ``$XDG_CACHE_HOME`` or ``~/.cache`` elsewhere).
    ``os_name`` defaults to ``os.name`` and is injectable for tests (so the branch
    can be exercised without breaking ``pathlib`` on the host platform).
    """
    os_name = os_name if os_name is not None else os.name
    if override:
        return Path(override).expanduser()
    env = os.environ.get("OPENMV_OTA_CACHE")
    if env:
        return Path(env).expanduser()
    if os_name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "openmv-ota" / "cache"
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else (Path.home() / ".cache")
    return base / "openmv-ota"


def firmware_clone_dir(commit: str, override: str | None = None) -> Path:
    """Per-commit clone directory, so projects pinned to the same commit share one."""
    return cache_root(override) / ("openmv-%s" % commit[:12])

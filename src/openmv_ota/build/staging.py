"""Copy an app tree into a temp staging dir before compiling/packing."""

from __future__ import annotations

import fnmatch
import shutil
from pathlib import Path

from openmv_ota.romfs.builder import DEFAULT_EXCLUDES

from .errors import BuildError


def stage_app(app_dir: Path, dest: Path) -> Path:
    """Copy ``app_dir`` into ``dest`` (must not exist), dropping the same junk the
    romfs builder excludes (``__pycache__``, ``*.pyc``, ``.git``, …)."""
    if not app_dir.is_dir():
        raise BuildError("app directory not found: %s" % app_dir)

    def ignore(_dir: str, names: list[str]) -> set[str]:
        return {n for n in names if any(fnmatch.fnmatch(n, p) for p in DEFAULT_EXCLUDES)}

    shutil.copytree(app_dir, dest, ignore=ignore)
    return dest


def iter_files(root: Path, suffixes: tuple[str, ...]):
    """Yield files under ``root`` whose lowercased suffix is in ``suffixes``."""
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in suffixes:
            yield path

"""Copy an app tree into a temp staging dir before compiling/packing."""

from __future__ import annotations

import fnmatch
import os
import shutil
import stat
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

    # A link is copied as what it points at, and what an app tree points at outside itself
    # (a key, a client token) would go into an image that is signed and published.
    for dirpath, dirs, files in os.walk(app_dir):
        skip = ignore(dirpath, dirs + files)
        for n in sorted(set(dirs + files) - skip):
            if os.path.islink(os.path.join(dirpath, n)):
                raise BuildError("%s is a symbolic link; an app directory holds files, "
                                 "not links" % os.path.join(dirpath, n), exit_code=1)
            # ...and files: a FIFO has the copy wait for a writer for ever, and a device or a
            # socket is no file to put in an image
            if n in files and not stat.S_ISREG(os.lstat(os.path.join(dirpath, n)).st_mode):
                raise BuildError("%s is not a regular file (a FIFO, a socket or a device); an "
                                 "app directory holds files" % os.path.join(dirpath, n),
                                 exit_code=1)
        dirs[:] = [d for d in dirs if d not in skip]
    shutil.copytree(app_dir, dest, ignore=ignore)
    return dest


def iter_files(root: Path, suffixes: tuple[str, ...]):
    """Yield files under ``root`` whose lowercased suffix is in ``suffixes``."""
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in suffixes:
            yield path

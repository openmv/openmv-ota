"""Compile ``.py`` -> ``.mpy`` with mpy-cross.

The IDE runs ``python -u -m mpy_cross <args> -o <out.mpy> <in.py>``. We do the
same: if the firmware build already produced an mpy-cross binary, use it; else
fall back to the pip-installed ``mpy_cross`` package (so a host C compiler isn't
needed — handy on Windows). The required version is detectable from the project:
the firmware's MicroPython version and ``.mpy`` ABI are in the lock.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import subprocess
import sys
from pathlib import Path

from ..errors import BuildError


def _has_pip_mpy_cross() -> bool:
    return importlib.util.find_spec("mpy_cross") is not None


def _pip_mpy_cross_version() -> str | None:
    try:
        return importlib.metadata.version("mpy-cross")
    except importlib.metadata.PackageNotFoundError:
        return None


def resolve_mpy_cross(project) -> list[str]:
    """Return the command prefix to invoke mpy-cross for ``project``.

    Prefers the firmware-built binary (exact); else the pip-installed
    ``mpy_cross`` (warning if its version doesn't match); else raises with the
    exact ``pip install`` command to run.
    """
    if project.mpy_cross_path:
        return [project.mpy_cross_path]

    mp = project.lock.micropython
    want = mp.get("version", "?")
    abi = "%s.%s" % (mp.get("mpy_abi_version"), mp.get("mpy_sub_version"))

    if _has_pip_mpy_cross():
        have = _pip_mpy_cross_version()
        if have and not have.startswith(want):
            print("warning: pip mpy-cross %s may not match firmware MicroPython %s "
                  "(.mpy ABI %s required)" % (have, want, abi), file=sys.stderr)
        return [sys.executable, "-m", "mpy_cross"]

    raise BuildError(
        "mpy-cross is not available. Install the matching version with "
        "`pip install mpy-cross==%s` (firmware MicroPython %s, .mpy ABI %s), or build "
        "the firmware, or pass --no-compile-py." % (want, want, abi),
        exit_code=1,
    )


def compile_py(mpy_cross: list[str], args: list[str], src: Path, out: Path) -> None:
    """Run ``<mpy_cross> <args> -o <out> <src>``. Raises on failure."""
    cmd = [*mpy_cross, *args, "-o", str(out), str(src)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        raise BuildError("mpy-cross not found: %s" % mpy_cross[0], exit_code=1) from None
    if proc.returncode != 0:
        raise BuildError(
            "mpy-cross failed on %s: %s" % (src.name, proc.stderr.strip()), exit_code=1
        )

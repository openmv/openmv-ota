"""Compile ``.py`` -> ``.mpy`` with the pegged mpy-cross.

The IDE runs ``python -u -m mpy_cross <args> -o <out.mpy> <in.py>``; the project's
mpy-cross binary takes the same arguments, so we invoke it directly. We do not
build mpy-cross ourselves — the project resolves the binary the firmware build
produced.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..errors import BuildError


def compile_py(mpy_cross: str, args: list[str], src: Path, out: Path) -> None:
    """Run ``<mpy_cross> <args> -o <out> <src>``. Raises on failure."""
    cmd = [mpy_cross, *args, "-o", str(out), str(src)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        raise BuildError("mpy-cross not found: %s" % mpy_cross, exit_code=1) from None
    if proc.returncode != 0:
        raise BuildError(
            "mpy-cross failed on %s: %s" % (src.name, proc.stderr.strip()), exit_code=1
        )

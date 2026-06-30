"""Run a flashing tool, turning a missing binary or a non-zero exit into a ``FlashError``.

This is the one side-effecting seam in the subsystem; tests monkeypatch it to assert the
argv the backends build without touching hardware.
"""

from __future__ import annotations

import subprocess

from .errors import FlashError


def run(argv: list[str]) -> None:
    """Run ``argv`` (streaming its output), raising ``FlashError`` on failure."""
    try:
        subprocess.run(argv, check=True)
    except FileNotFoundError:
        raise FlashError("%s not found -- is it installed?" % argv[0], exit_code=1) from None
    except subprocess.CalledProcessError as e:
        raise FlashError("flashing failed (%s): exit %d" % (argv[0], e.returncode),
                         exit_code=1) from None

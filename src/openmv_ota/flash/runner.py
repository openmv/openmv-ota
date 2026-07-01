"""Run a flashing tool, turning a missing binary or a non-zero exit into a ``FlashError``.

This is the one side-effecting seam in the subsystem; tests monkeypatch it to assert the
argv the backends build without touching hardware.
"""

from __future__ import annotations

import subprocess
import sys

from .errors import FlashError


def output(argv: list[str]) -> str:
    """Run ``argv`` and return its captured stdout -- for read-only *queries* (``dfu-util -l``,
    the spsdk USB scan) rather than flashing. ``FlashError`` on a missing binary or non-zero
    exit; the one other side-effecting seam tests monkeypatch."""
    try:
        return subprocess.run(argv, check=True, capture_output=True, text=True).stdout
    except FileNotFoundError:
        raise FlashError("%s not found -- is it installed?" % argv[0], exit_code=1) from None
    except subprocess.CalledProcessError as e:
        raise FlashError("%s failed: exit %d" % (argv[0], e.returncode), exit_code=1) from None


def run(argv: list[str], *, tolerate_fail: bool = False) -> None:
    """Run ``argv`` (streaming its output), raising ``FlashError`` on failure. With
    ``tolerate_fail`` a non-zero exit is warned about and ignored -- for the system-DFU
    bootloader write, whose ST ROM doesn't ACK the final status (so dfu-util exits non-zero
    even when the write succeeded)."""
    try:
        subprocess.run(argv, check=True)
    except FileNotFoundError:
        raise FlashError("%s not found -- is it installed?" % argv[0], exit_code=1) from None
    except subprocess.CalledProcessError as e:
        if tolerate_fail:
            print("warning: %s exited %d -- continuing (expected for this step)"
                  % (argv[0], e.returncode), file=sys.stderr)
            return
        raise FlashError("flashing failed (%s): exit %d" % (argv[0], e.returncode),
                         exit_code=1) from None

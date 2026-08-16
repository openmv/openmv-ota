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


_DFU_RESET_SUCCESS_MARKERS = (
    "Download done.",
    "Done!",
    "Resetting USB to switch back to Run-Time mode",
)


def run(argv: list[str], *, tolerate_fail: bool = False,
        accept_dfu_reset_disconnect: bool = False) -> None:
    """Run ``argv`` (streaming its output), raising ``FlashError`` on failure. With
    ``tolerate_fail`` a non-zero exit is warned about and ignored -- for the system-DFU
    bootloader write, whose ST ROM doesn't ACK the final status (so dfu-util exits non-zero
    even when the write succeeded)."""
    try:
        if accept_dfu_reset_disconnect:
            result = subprocess.run(argv, check=False, capture_output=True, text=True)
            sys.stdout.write(result.stdout)
            sys.stderr.write(result.stderr)
            if result.returncode == 0:
                return
            transcript = result.stdout + result.stderr
            if (result.returncode == 251
                    and all(marker in transcript for marker in _DFU_RESET_SUCCESS_MARKERS)):
                print("warning: %s exited 251 after a verified DFU transfer and USB reset"
                      % argv[0], file=sys.stderr)
                return
            raise subprocess.CalledProcessError(result.returncode, argv)
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

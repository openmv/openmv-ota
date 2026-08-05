"""`_uart_live_since_flash` -- "the marker UART is live" must mean SINCE THIS FLASH.

`_CAP.raw` is the capture's whole history, so it still holds what the PREVIOUS firmware wrote. Two
places read it as proof the board is logging to the marker UART: the bench-file write (skipped when
"live") and the guard that refuses to score a silent board. On stale evidence both wave through a
board that went silent at the flash -- which failed a real N6 leg 28 minutes later with every
device marker missing, while the board answered its REPL the whole time.
"""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ci" / "hil"))

import ota_cycle  # noqa: E402


def _set(lines, flash_mark):
    ota_cycle._CAP = types.SimpleNamespace(raw=list(lines))
    ota_cycle._FLASH_MARK = flash_mark


def test_lines_from_before_the_flash_do_not_count():
    """The exact failure: the old firmware chattered, the new one is silent."""
    _set(["boot: ready", "app: device_id abc", "wdt: feed"], flash_mark=3)
    assert ota_cycle._uart_live_since_flash() is False


def test_lines_after_the_flash_count():
    _set(["boot: ready", "app: device_id abc", "boot: ready"], flash_mark=2)
    assert ota_cycle._uart_live_since_flash() is True


def test_no_flash_yet_means_the_whole_capture_counts():
    """_FLASH_MARK starts at 0, so a pre-flash caller sees everything -- the old behaviour."""
    _set(["boot: ready"], flash_mark=0)
    assert ota_cycle._uart_live_since_flash() is True


def test_a_silent_capture_is_not_live():
    _set([], flash_mark=0)
    assert ota_cycle._uart_live_since_flash() is False


def test_no_capture_at_all_is_not_live():
    ota_cycle._CAP, ota_cycle._FLASH_MARK = None, 0
    assert ota_cycle._uart_live_since_flash() is False

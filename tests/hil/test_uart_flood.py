"""The marker UART is a firehose when a board goes wrong. This is the valve.

A Portenta left in a REPL soft-reset loop emitted `MPY: soft reboot` / `OK` at line
rate for five minutes: 9,293,499 lines, an 838 MB job log, every line scanned against
96 marker substrings, and the capture's `raw` list growing without bound in the
runner's RAM. The board itself was fine -- the golden reflash cleared it -- but what
actually happened was buried under nine million lines of noise.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..", "..", "ci", "hil")))
os.environ.setdefault("WIFI_SSID", "")
os.environ.setdefault("WIFI_PASSWORD", "")

import ota_cycle  # noqa: E402


class _Capture(ota_cycle.UartCapture):
    """The capture's line handling without a serial port under it."""

    def __init__(self):
        self.markers, self.raw, self.flooded = [], [], 0
        self._window, self._seen_in_window, self._dropped = 0.0, 0, 0
        self._t0 = 0.0


def test_a_flooding_board_does_not_produce_a_million_line_log(capsys):
    cap = _Capture()
    kept = [line for line in ["MPY: soft reboot", "OK"] * 5000 if not cap._flooding(line)]
    assert len(kept) <= ota_cycle._UART_LINES_PER_SEC + 1     # one window's worth, then counted
    assert cap._dropped > 9000
    # ...and the drop is reported rather than silent: silence would read as a dead board
    cap._window = 0.0
    cap._flooding("MPY: soft reboot")
    assert "REPL noise suppressed" in capsys.readouterr().out


def test_the_boards_own_log_lines_are_never_dropped():
    """A board can be flooding AND working -- the Portenta was logging real markers on the
    same UART the noise came out of. Scoring must not depend on the noise level."""
    cap = _Capture()
    for _ in range(5000):
        cap._flooding("OK")
    marker = "[2026-09-17 14:23:23] INFO openmv_ota: boot: mounted A (payload 16777216)"
    assert cap._flooding(marker) is False


def test_an_ordinary_boards_output_is_untouched():
    cap = _Capture()
    lines = ["[t] DEBUG openmv_ota: run: poll wait", "MPY: soft reboot", "OK", "anything else"]
    assert [cap._flooding(line) for line in lines] == [False, False, False, False]

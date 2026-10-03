"""A second reader on the marker UART steals lines, and the run then blames the board.

A tty hands each byte to whichever ``read()`` wins. Measured on RT1060 wifi `delta`: a leftover
bench capture had been reading /dev/ttyUSB0 for four hours; the device installed, trialled,
confirmed and promoted, and the leg failed with 14 markers missing -- every one of them in the
other reader's log. pyserial only reports it sideways, as a "multiple access" read error, which
the capture used to swallow as a dropped port. These tests pin the claim (TIOCEXCL), the count,
and the line that names it on a FAIL.
"""

from __future__ import annotations

import errno
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..", "..", "ci", "hil")))
os.environ.setdefault("WIFI_SSID", "")
os.environ.setdefault("WIFI_PASSWORD", "")

import ota_cycle  # noqa: E402

_PYSERIAL_MSG = ("device reports readiness to read but returned no data (device disconnected or "
                 "multiple access on port?)")


class _Capture(ota_cycle.UartCapture):
    """The capture's bookkeeping without a serial port under it."""

    def __init__(self):
        self._port = "/dev/ttyUSB0"
        self.contended = 0


def test_a_stolen_read_is_counted_and_named_once(monkeypatch):
    said = []
    monkeypatch.setattr(ota_cycle, "log", said.append)
    cap = _Capture()
    for _ in range(3):
        cap._note_contention(Exception(_PYSERIAL_MSG))
    assert cap.contended == 3
    assert len(said) == 1 and "ANOTHER PROCESS IS READING /dev/ttyUSB0" in said[0]


def test_a_real_port_error_is_not_contention(monkeypatch):
    said = []
    monkeypatch.setattr(ota_cycle, "log", said.append)
    cap = _Capture()
    cap._note_contention(OSError(errno.EIO, "Input/output error"))
    assert cap.contended == 0 and said == []


def test_a_fail_says_the_uart_was_shared(monkeypatch):
    said = []
    monkeypatch.setattr(ota_cycle, "log", said.append)
    cap = _Capture()
    ota_cycle._log_contention(cap)
    ota_cycle._log_contention(None)
    assert said == []                        # an unshared port adds nothing to a FAIL
    cap.contended = 5
    ota_cycle._log_contention(cap)
    assert len(said) == 1 and "5 stolen read(s)" in said[0] and "not a verdict" in said[0]


class _Fd:
    def __init__(self, fd):
        self._fd = fd

    def fileno(self):
        return self._fd


@pytest.mark.skipif(not hasattr(os, "openpty") or os.geteuid() == 0,
                    reason="needs a pty, and root ignores TIOCEXCL")
def test_the_claim_locks_out_a_later_opener_and_lifts_cleanly():
    master, slave = os.openpty()
    path = os.ttyname(slave)
    try:
        ota_cycle._claim_port(_Fd(slave))
        with pytest.raises(OSError) as e:
            os.open(path, os.O_RDWR | os.O_NOCTTY)
        assert e.value.errno == errno.EBUSY  # the interloper's reopen is refused
        ota_cycle._claim_port(_Fd(slave), False)
        os.close(os.open(path, os.O_RDWR | os.O_NOCTTY))   # ...and our own reopen is not
    finally:
        os.close(slave)
        os.close(master)


def test_the_claim_is_best_effort():
    ota_cycle._claim_port(object())          # no fileno: left alone, never raises

"""The marker UART is addressed by a ``ttyUSBn`` name, and Linux hands those out in PLUG ORDER --
so re-cabling a node moves /dev/ttyUSB0 to ttyUSB1 and every scenario then fails with "could not
open port" and ``coverage 0/N``. That looks exactly like a dead board, so it costs a debugging
cycle every time. ``resolve_uart`` falls back to the node's single USB-serial bridge; these tests
pin the fallback AND its refusal to guess when the choice is ambiguous.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..", "..", "ci", "hil")))
os.environ.setdefault("WIFI_SSID", "")
os.environ.setdefault("WIFI_PASSWORD", "")

import ota_cycle  # noqa: E402  (ci/hil, added to sys.path above)


class _Port:
    def __init__(self, device, vid=0x10C4):
        self.device, self.vid = device, vid


def _fake_comports(monkeypatch, ports):
    from serial.tools import list_ports
    monkeypatch.setattr(list_ports, "comports", lambda: ports)


def test_keeps_the_configured_path_when_it_exists(monkeypatch):
    # the normal case: BOARD_UART is right, so nothing is probed and nothing changes
    monkeypatch.setattr(ota_cycle.os.path, "exists", lambda p: True)
    assert ota_cycle.resolve_uart("/dev/ttyUSB0") == "/dev/ttyUSB0"


def test_falls_back_to_the_only_bridge_when_renumbered(monkeypatch):
    # the RT node after a re-plug: configured ttyUSB0 is gone, the bridge came back as ttyUSB1
    monkeypatch.setattr(ota_cycle.os.path, "exists", lambda p: False)
    _fake_comports(monkeypatch, [_Port("/dev/ttyUSB1")])
    assert ota_cycle.resolve_uart("/dev/ttyUSB0") == "/dev/ttyUSB1"


def test_does_not_guess_between_two_bridges(monkeypatch):
    # two adapters -> picking one could capture the WRONG board's markers, which would be a
    # silently wrong PASS/FAIL. Keep the configured path so the real error surfaces instead.
    monkeypatch.setattr(ota_cycle.os.path, "exists", lambda p: False)
    _fake_comports(monkeypatch, [_Port("/dev/ttyUSB1"), _Port("/dev/ttyUSB2")])
    assert ota_cycle.resolve_uart("/dev/ttyUSB0") == "/dev/ttyUSB0"


def test_does_not_fall_back_when_nothing_is_attached(monkeypatch):
    monkeypatch.setattr(ota_cycle.os.path, "exists", lambda p: False)
    _fake_comports(monkeypatch, [])
    assert ota_cycle.resolve_uart("/dev/ttyUSB0") == "/dev/ttyUSB0"


def test_ignores_non_usb_serial_devices(monkeypatch):
    # a motherboard ttyS0 has no USB vid -- it must never be mistaken for the marker bridge
    monkeypatch.setattr(ota_cycle.os.path, "exists", lambda p: False)
    _fake_comports(monkeypatch, [_Port("/dev/ttyS0", vid=None), _Port("/dev/ttyUSB1")])
    assert ota_cycle.resolve_uart("/dev/ttyUSB0") == "/dev/ttyUSB1"


def test_capture_frees_a_stale_holder_before_opening(monkeypatch):
    """A cancelled run leaves its capture thread holding the marker UART. A second reader does NOT
    get a copy of the stream -- the bytes go to whoever wins the read -- so the next run sees a
    partial stream or none, waits out its whole timeout, and fails a board that is logging
    perfectly. Measured on the Nicla node: a `runner` python from a cancelled run still held
    /dev/ttyUSB0. The CDC path already did this via _ensure_cdc's fuser -k; the UART never did."""
    src = open(os.path.join(_HERE, "..", "..", "ci", "hil", "ota_cycle.py")).read()
    body = src.split("class UartCapture:")[1].split("    def start(")[0]
    assert "fuser -k" in body, "the capture must free a stale holder before it opens the port"
    assert body.index("fuser -k") < body.index("serial.Serial("), "free it BEFORE opening"


def test_run_refuses_to_score_a_dead_marker_uart():
    """Every scenario is scored on the marker UART. If it is dead the run burns its whole timeout
    and then reports missing markers -- which reads as a broken device and is not. Observed: a leg
    whose .hilcov_uart never landed produced ZERO device lines for 25 minutes, then failed with
    boot.ready/log.configured missing, on a board that was fine and logging to USB.

    Fail before the scored window opens, and say what to check."""
    src = open(os.path.join(_HERE, "..", "..", "ci", "hil", "ota_cycle.py")).read()
    # anchor to the TOP-LEVEL def: the generated bench app contains "async def main():" too, and
    # splitting on the bare name grabs that instead
    body = src.split("\ndef main(")[1]
    guard = body.index("if not cap.raw:")
    reset = body.index("cap.reset(time.time())")
    assert guard < reset, "the check must run BEFORE the scored window starts"
    assert ".hilcov_uart" in body[guard:reset], "the error must name the file to check"

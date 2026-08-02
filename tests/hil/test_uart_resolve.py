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

"""Camera discovery + reset-into-bootloader (mpremote / 1200-baud touch), hardware-free."""

from __future__ import annotations

import pytest

from openmv_ota.flash import device
from openmv_ota.flash.errors import FlashError
from openmv_ota.flash.targets import flash_config


class _Port:
    def __init__(self, vid, pid, dev, serial=None):
        self.vid, self.pid, self.device, self.serial_number = vid, pid, dev, serial


def _dfu_raw():
    return flash_config("OPENMV4").raw          # runtime 37c5:1204


def _arduino_raw():
    return flash_config("ARDUINO_PORTENTA_H7").raw


def test_runtime_ids_openmv_single():
    assert device.runtime_ids(_dfu_raw()) == {(0x37C5, 0x1204)}


def test_runtime_ids_arduino_multiple():
    ids = device.runtime_ids(_arduino_raw())
    assert (0x2341, 0x005B) in ids and (0x2341, 0x025B) in ids   # base usb + a touch pid
    assert all(vid == 0x2341 for vid, _ in ids)


def test_runtime_ids_none_when_unconfigured():
    assert device.runtime_ids({}) == set()


def test_discover_matches_runtime_vid_pid(monkeypatch):
    monkeypatch.setattr(device, "_comports", lambda: [
        _Port(0x1234, 0x0001, "/dev/other"),
        _Port(0x37C5, 0x1204, "/dev/ttyACM0", "SN1")])
    cams = device.discover(_dfu_raw())
    assert len(cams) == 1 and cams[0].port == "/dev/ttyACM0" and cams[0].serial == "SN1"


def test_select_one(monkeypatch):
    monkeypatch.setattr(device, "_comports", lambda: [_Port(0x37C5, 0x1204, "/dev/a", "SN1")])
    assert device.select(_dfu_raw(), None).serial == "SN1"


def test_select_none_when_no_camera(monkeypatch):
    monkeypatch.setattr(device, "_comports", lambda: [])     # already in bootloader / unplugged
    assert device.select(_dfu_raw(), None) is None


def test_select_filters_by_serial(monkeypatch):
    monkeypatch.setattr(device, "_comports", lambda: [
        _Port(0x37C5, 0x1204, "/dev/a", "SN1"), _Port(0x37C5, 0x1204, "/dev/b", "SN2")])
    assert device.select(_dfu_raw(), "SN2").port == "/dev/b"


def test_select_multiple_without_serial_errors(monkeypatch):
    monkeypatch.setattr(device, "_comports", lambda: [
        _Port(0x37C5, 0x1204, "/dev/a", "SN1"), _Port(0x37C5, 0x1204, "/dev/b", "SN2")])
    with pytest.raises(FlashError, match="multiple cameras"):
        device.select(_dfu_raw(), None)


def test_reset_openmv_runs_mpremote(monkeypatch):
    from openmv_ota.flash import runner
    ran = []
    monkeypatch.setattr(runner, "run", lambda argv: ran.append(argv))
    device.reset(_dfu_raw(), device.Camera("/dev/ttyACM0", "SN1"), mpremote=["mpremote"])
    assert ran == [["mpremote", "connect", "/dev/ttyACM0", "bootloader"]]


def test_reset_arduino_touches_1200(monkeypatch):
    from openmv_ota.flash import runner
    opened = []
    monkeypatch.setattr(device, "_open_1200", lambda port: opened.append(port))
    monkeypatch.setattr(runner, "run",
                        lambda argv: (_ for _ in ()).throw(AssertionError("no mpremote")))
    device.reset(_arduino_raw(), device.Camera("/dev/ttyACM0", None), mpremote=["mpremote"])
    assert opened == ["/dev/ttyACM0"]


def test_comports_wraps_pyserial(monkeypatch):
    monkeypatch.setattr("serial.tools.list_ports.comports", lambda: ["p0"])
    assert device._comports() == ["p0"]


def test_open_1200_opens_at_1200_and_closes(monkeypatch):
    import serial
    made = {}

    class _FakeSerial:
        def __init__(self, port, baud):
            made["port"], made["baud"], made["closed"] = port, baud, False

        def close(self):
            made["closed"] = True

    monkeypatch.setattr(serial, "Serial", _FakeSerial)
    device._open_1200("/dev/ttyACM0")
    assert made == {"port": "/dev/ttyACM0", "baud": 1200, "closed": True}

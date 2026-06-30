"""The Arduino address-based dfu plan + 1200-baud touch-to-reset (hardware-free)."""

from __future__ import annotations

from openmv_ota.flash import arduino
from openmv_ota.flash.targets import flash_config


def _raw():
    return flash_config("ARDUINO_PORTENTA_H7").raw


def test_program_argv_address_and_leave():
    from pathlib import Path
    assert arduino.program_argv("dfu-util", "2341:035b", 0, "0x08040000", Path("fw.bin")) == \
        ["dfu-util", "-w", "-d", ",2341:035b", "-a", "0", "-s", "0x08040000", "-D", "fw.bin"]
    leave = arduino.program_argv("dfu-util", "2341:035b", 1, "0x90B00000", Path("r.img"),
                                 leave=True)
    assert leave[6:8] == ["-s", "0x90B00000:leave"]


def test_firmware_plan_is_one_leave_write(tmp_path):
    files = {"firmware": tmp_path / "fw.bin"}
    steps = arduino.plan("firmware", _raw(), "dfu-util", files)
    assert len(steps) == 1
    assert steps[0].argv[6:8] == ["-s", "0x08040000:leave"]


def test_romfs_plan_targets_qspi(tmp_path):
    files = {"romfs": tmp_path / "r.img"}
    steps = arduino.plan("romfs", _raw(), "dfu-util", files)
    assert steps[0].argv[6:8] == ["-s", "0x90B00000:leave"]


def test_factory_plan_writes_wifi_firmware_romfs_leave_last(tmp_path):
    files = {"firmware": tmp_path / "fw.bin", "romfs": tmp_path / "r.img",
             "wifi": [tmp_path / "cyw0.bin", tmp_path / "cyw1.bin"]}
    steps = arduino.plan("factory", _raw(), "dfu-util", files)
    addrs = [s.argv[7] for s in steps]
    assert addrs == ["0x90F00000", "0x90FC0000", "0x08040000", "0x90B00000:leave"]
    assert sum(a.endswith(":leave") for a in addrs) == 1   # only the final write leaves DFU


def test_touch_to_reset_pulses_matching_app_port(monkeypatch):
    class _Port:
        def __init__(self, vid, pid, device):
            self.vid, self.pid, self.device = vid, pid, device
    opened = []
    slept = []
    monkeypatch.setattr(arduino, "_comports", lambda: [
        _Port(0x1234, 0x0001, "/dev/other"),                 # unrelated device
        _Port(0x2341, 0x005b, "/dev/ttyACM0")])              # the Portenta in app mode
    monkeypatch.setattr(arduino, "_open_1200", lambda port: opened.append(port))
    monkeypatch.setattr(arduino.time, "sleep", lambda s: slept.append(s))
    assert arduino.touch_to_reset(_raw()) == "/dev/ttyACM0"
    assert opened == ["/dev/ttyACM0"] and slept == [arduino._TOUCH_SETTLE_S]


def test_touch_to_reset_noop_when_not_in_app_mode(monkeypatch):
    monkeypatch.setattr(arduino, "_comports", lambda: [])    # board already in the bootloader
    monkeypatch.setattr(arduino, "_open_1200",
                        lambda port: (_ for _ in ()).throw(AssertionError("should not open")))
    assert arduino.touch_to_reset(_raw()) is None


def test_touch_to_reset_noop_without_app_block():
    assert arduino.touch_to_reset({"usb": "2341:035b"}) is None   # no app -> nothing to touch


def test_comports_wraps_pyserial(monkeypatch):
    monkeypatch.setattr("serial.tools.list_ports.comports", lambda: ["p0"])
    assert arduino._comports() == ["p0"]


def test_open_1200_opens_at_1200_and_closes(monkeypatch):
    import serial
    made = {}

    class _FakeSerial:
        def __init__(self, port, baud):
            made["port"], made["baud"], made["closed"] = port, baud, False

        def close(self):
            made["closed"] = True

    monkeypatch.setattr(serial, "Serial", _FakeSerial)
    arduino._open_1200("/dev/ttyACM0")
    assert made == {"port": "/dev/ttyACM0", "baud": 1200, "closed": True}

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


def test_program_argv_never_pins_the_serial():
    """A board's DFU-mode serial is NOT its runtime serial, so pinning -S matches nothing and the
    `-w` wait hangs forever -- a flash that appears to freeze rather than fail. Measured on a
    Portenta H7: runtime 346534563033 (2341:045b) vs DFU 0033001F3033510634323437 (2341:035b).
    That cost a 1500 s timeout on the Nicla. (These tests previously asserted the OPPOSITE and so
    pinned the bug in place; flash/dfu.py::download_argv had already dropped -S for this reason.)"""
    from pathlib import Path
    argv = arduino.program_argv("dfu-util", "2341:035b", 0, "0x08040000", Path("fw.bin"),
                                serial="AB12")
    assert "-S" not in argv and "AB12" not in argv
    # -d ,<vid:pid> still selects the bootloader, and the 1200-baud touch put only THIS board in DFU
    assert argv[1:4] == ["-w", "-d", ",2341:035b"]


def test_plan_never_threads_serial_into_the_argv():
    files = {"firmware": __import__("pathlib").Path("fw.bin")}
    steps = arduino.plan("firmware", _raw(), "dfu-util", files, serial="AB12")
    assert "-S" not in steps[0].argv and "AB12" not in steps[0].argv

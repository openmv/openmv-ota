"""Getting a board into DFU, and only once every precheck has passed (bring-up regressions).

* The N6 failed ``dfu-util -w`` 3 of 3 times: ``-w`` opens the device the instant libusb sees it,
  before the bootloader has finished enumerating. The first write now waits for the device to be
  LISTED, settles, and runs without ``-w``.
* Every verb used to reset the board into its bootloader BEFORE resolving dfu-util or the
  artifacts, so a missing tool stranded the board in DFU (a Portenta until power-cycled).
* An Arduino app with an armed 100 ms watchdog reboots before the 1200-baud touch lands, and a
  DfuSe device in dfuERROR stalls every write: both are retried once, then explained.
"""

from __future__ import annotations

import pytest

from openmv_ota.cli import main
from openmv_ota.flash import flash as fl
from openmv_ota.flash.errors import FlashError
from openmv_ota.flash.targets import flash_config

_LISTING = ('Found DFU: [37c5:9206] ver=0200, devnum=9, cfg=1, intf=0, path="1-2", alt=1, '
            'name="@Flash", serial="0123"\n')


class _Clock:
    """A fake monotonic clock that sleep() advances, so the bounded waits run instantly."""

    def __init__(self, monkeypatch):
        self.t = 0.0
        self.sleeps = []
        monkeypatch.setattr(fl.time, "monotonic", lambda: self.t)
        monkeypatch.setattr(fl.time, "sleep", self.sleep)

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


# --- the bounded DFU wait ------------------------------------------------------------------

def test_dfu_listed_matches_the_vid_pid_in_a_dfu_util_listing(monkeypatch):
    monkeypatch.undo()                                   # the conftest stub answers True
    monkeypatch.setattr(fl.runner, "output", lambda argv: _LISTING)
    assert fl._dfu_listed("dfu-util", "37c5:9206") is True
    assert fl._dfu_listed("dfu-util", "37C5:9206") is True          # case-insensitive
    assert fl._dfu_listed("dfu-util", "2341:035b") is False


def test_dfu_listed_treats_a_failed_scan_as_nothing_listed(monkeypatch):
    monkeypatch.undo()

    def boom(argv):
        raise FlashError("dfu-util failed: exit 74")
    monkeypatch.setattr(fl.runner, "output", boom)
    assert fl._dfu_listed("dfu-util", "37c5:9206") is False


def test_await_dfu_polls_until_listed_then_settles(monkeypatch):
    clock = _Clock(monkeypatch)
    seen = iter([False, False, True])
    monkeypatch.setattr(fl, "_dfu_listed", lambda tool, usb: next(seen))
    assert fl._await_dfu("dfu-util", "37c5:9206") is True
    assert clock.sleeps == [fl._DFU_POLL_S, fl._DFU_POLL_S, fl._DFU_SETTLE_S]


def test_await_dfu_is_bounded(monkeypatch):
    _Clock(monkeypatch)
    monkeypatch.setattr(fl, "_dfu_listed", lambda tool, usb: False)
    assert fl._await_dfu("dfu-util", "37c5:9206", timeout=1) is False


def test_no_device_is_a_clear_error_not_a_hang(tmp_path, monkeypatch, capsys):
    _Clock(monkeypatch)
    monkeypatch.setattr(fl, "_dfu_listed", lambda tool, usb: False)
    monkeypatch.setattr(fl.runner, "run", lambda argv, **k: pytest.fail("nothing may run"))
    monkeypatch.setattr(fl.tools, "find_dfu_util", lambda override, sdk_home: "DFU")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "OPENMV_N6-firmware.bin").write_bytes(b"x")
    with pytest.raises(FlashError, match=r"no DFU device \(37c5:9206\) appeared.*--in-bootloader"):
        fl.flash_firmware(str(tmp_path), board="OPENMV_N6", enter_bootloader=False)


def test_a_reset_that_really_failed_shows_what_mpremote_said(monkeypatch):
    # the reboot step's traceback is held back as the expected USB drop -- but when the bootloader
    # then NEVER appears, it is the evidence, so the timeout error carries its tail
    _Clock(monkeypatch)
    monkeypatch.setattr(fl, "_dfu_listed", lambda tool, usb: False)
    monkeypatch.setattr(fl.device, "_comports",
                        lambda: [_Port(0x37C5, 0x1206, "/dev/ttyACM0", "SN")])
    out = "\n".join("line %d" % i for i in range(20)) + "\nmpremote: could not enter raw repl"
    monkeypatch.setattr(fl.device, "reset", lambda *a, **k: out)
    raw = fl.flash_config("OPENMV_N6").raw
    fl._prepare(raw, serial=None, enter_bootloader=True, mpremote=None, dry_run=False)
    with pytest.raises(FlashError) as e:
        fl._ensure_dfu("DFU", "37c5:9206", "OPENMV_N6")
    msg = str(e.value)
    assert "the reboot-into-bootloader step said:" in msg
    assert "could not enter raw repl" in msg and "line 16" in msg and "line 15" not in msg
    assert fl._reset_output is None          # consumed: a later, unrelated wait doesn't repeat it


def test_a_reset_whose_bootloader_appears_shows_nothing(monkeypatch):
    _Clock(monkeypatch)
    monkeypatch.setattr(fl, "_dfu_listed", lambda tool, usb: True)
    monkeypatch.setattr(fl, "_reset_output", "Traceback ... OSError: [Errno 5]")
    fl._ensure_dfu("DFU", "37c5:9206", "OPENMV_N6")      # no raise: the EIO was the expected drop
    assert fl._reset_output is None


# --- prechecks before the reset ------------------------------------------------------------

class _Port:
    def __init__(self, vid, pid, dev, serial=None):
        self.vid, self.pid, self.device, self.serial_number = vid, pid, dev, serial


@pytest.mark.parametrize("board, port", [
    ("OPENMV_N6", _Port(0x37C5, 0x1206, "/dev/ttyACM0", "SN")),
    ("ARDUINO_PORTENTA_H7", _Port(0x2341, 0x045B, "/dev/ttyACM0", "SN")),
])
def test_a_missing_tool_never_resets_the_board(tmp_path, monkeypatch, board, port):
    """Measured: the reset ran first, dfu-util was then not found, and the board sat in DFU."""
    monkeypatch.setattr(fl.device, "_comports", lambda: [port])
    monkeypatch.setattr(fl.device, "reset", lambda *a, **k: pytest.fail("reset before precheck"))

    def missing(override, sdk_home):
        raise FlashError("dfu-util not found")
    monkeypatch.setattr(fl.tools, "find_dfu_util", missing)
    with pytest.raises(FlashError, match="dfu-util not found"):
        fl.flash_factory(str(tmp_path), board=board)


@pytest.mark.parametrize("board, port", [
    ("OPENMV_N6", _Port(0x37C5, 0x1206, "/dev/ttyACM0", "SN")),
    ("ARDUINO_NICLA_VISION", _Port(0x2341, 0x045F, "/dev/ttyACM0", "SN")),
])
def test_a_missing_artifact_never_resets_the_board(tmp_path, monkeypatch, board, port):
    (tmp_path / "build").mkdir()
    monkeypatch.setattr(fl.device, "_comports", lambda: [port])
    monkeypatch.setattr(fl.device, "reset", lambda *a, **k: pytest.fail("reset before precheck"))
    monkeypatch.setattr(fl.tools, "find_dfu_util", lambda override, sdk_home: "DFU")
    with pytest.raises(FlashError, match="missing"):
        fl.flash_factory(str(tmp_path), board=board)


def test_erase_resolves_the_tool_before_the_reset(tmp_path, monkeypatch):
    monkeypatch.setattr(fl.device, "_comports",
                        lambda: [_Port(0x37C5, 0x1206, "/dev/ttyACM0", "SN")])
    monkeypatch.setattr(fl.device, "reset", lambda *a, **k: pytest.fail("reset before precheck"))

    def missing(override, sdk_home):
        raise FlashError("dfu-util not found")
    monkeypatch.setattr(fl.tools, "find_dfu_util", missing)
    with pytest.raises(FlashError, match="dfu-util not found"):
        fl.flash_erase(str(tmp_path), board="OPENMV_N6")


def test_dfu_flash_resets_then_waits_then_writes(tmp_path, monkeypatch):
    order = []
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "OPENMV_N6-firmware.bin").write_bytes(b"x")
    monkeypatch.setattr(fl.tools, "find_dfu_util", lambda override, sdk_home: "DFU")
    monkeypatch.setattr(fl.history, "record", lambda *a, **k: None)
    monkeypatch.setattr(fl.device, "_comports",
                        lambda: [_Port(0x37C5, 0x1206, "/dev/ttyACM0", "SN")])
    monkeypatch.setattr(fl.device, "reset", lambda *a, **k: order.append("reset"))
    monkeypatch.setattr(fl, "_await_dfu", lambda tool, usb, *a: order.append("wait") or True)
    monkeypatch.setattr(fl.runner, "run", lambda argv, **k: order.append(argv[0]))
    fl.flash_firmware(str(tmp_path), board="OPENMV_N6")
    assert order == ["reset", "wait", "DFU"]


# --- Arduino: DFU entry ----------------------------------------------------------------------

_NICLA = flash_config("ARDUINO_NICLA_VISION").raw
_CAM = fl.device.Camera("/dev/ttyACM0", "SN")


def _arduino(monkeypatch, *, cams, dfu):
    """Stub the camera scan (successive answers), the touch, and the DFU wait."""
    touches = []
    cams = iter(cams)
    dfu = iter(dfu)
    monkeypatch.setattr(fl, "_await_camera", lambda raw, serial, timeout: next(cams))
    monkeypatch.setattr(fl.device, "reset", lambda raw, cam, **k: touches.append(cam.port))
    monkeypatch.setattr(fl, "_await_dfu", lambda tool, usb, *a: next(dfu))
    return touches


def test_arduino_touch_that_lands_enters_dfu(monkeypatch):
    touches = _arduino(monkeypatch, cams=[_CAM], dfu=[True])
    fl._arduino_enter("ARDUINO_NICLA_VISION", _NICLA, "DFU", None, True)
    assert touches == ["/dev/ttyACM0"]


def test_arduino_touch_is_retried_once_the_app_is_back(monkeypatch, capsys):
    """An armed 100 ms watchdog bites during the touch's 250 ms jump delay: the app reboots."""
    touches = _arduino(monkeypatch, cams=[_CAM, _CAM], dfu=[False, True])
    fl._arduino_enter("ARDUINO_NICLA_VISION", _NICLA, "DFU", None, True)
    assert len(touches) == 2
    assert "did not enter DFU after the 1200-baud touch (attempt 1/2)" in capsys.readouterr().err


def test_arduino_touch_that_never_lands_says_double_tap(monkeypatch):
    _arduino(monkeypatch, cams=[_CAM, _CAM], dfu=[False, False])
    with pytest.raises(FlashError, match="armed watchdog.*double-tap the board's reset"):
        fl._arduino_enter("ARDUINO_NICLA_VISION", _NICLA, "DFU", None, True)


def test_arduino_with_no_app_running_waits_for_dfu(monkeypatch):
    touches = _arduino(monkeypatch, cams=[None], dfu=[True])   # already in DFU (double-tapped)
    fl._arduino_enter("ARDUINO_NICLA_VISION", _NICLA, "DFU", None, True)
    assert touches == []


def test_arduino_app_gone_after_a_failed_touch_falls_back_to_the_wait(monkeypatch):
    touches = _arduino(monkeypatch, cams=[_CAM, None], dfu=[False, True])
    fl._arduino_enter("ARDUINO_NICLA_VISION", _NICLA, "DFU", None, True)
    assert len(touches) == 1


def test_arduino_in_bootloader_with_no_device_says_double_tap(monkeypatch):
    _arduino(monkeypatch, cams=[], dfu=[False])
    with pytest.raises(FlashError, match="no DFU device.*double-tap"):
        fl._arduino_enter("ARDUINO_NICLA_VISION", _NICLA, "DFU", None, False)


def test_await_camera_polls_until_the_port_is_back(monkeypatch):
    _Clock(monkeypatch)
    answers = iter([None, None, _CAM])
    monkeypatch.setattr(fl.device, "select", lambda raw, serial: next(answers))
    assert fl._await_camera(_NICLA, None, 10) == _CAM
    monkeypatch.setattr(fl.device, "select", lambda raw, serial: None)
    assert fl._await_camera(_NICLA, None, 1) is None


# --- Arduino: a wedged DFU device is retried once from a fresh entry --------------------------

@pytest.fixture
def nicla(tmp_path, monkeypatch):
    (tmp_path / "build").mkdir()
    for n in ("ARDUINO_NICLA_VISION-firmware.bin", "ARDUINO_NICLA_VISION-factory-romfs.img",
              "cyw4343_7_45_98_102.bin", "cyw4343_btfw.bin"):
        (tmp_path / "build" / n).write_bytes(b"x")
    monkeypatch.setattr(fl.tools, "find_dfu_util", lambda override, sdk_home: "DFU")
    monkeypatch.setattr(fl.history, "record", lambda *a, **k: None)
    entries = []
    monkeypatch.setattr(fl, "_arduino_enter",
                        lambda board, raw, tool, serial, enter, first_wait=0:
                        entries.append((enter, first_wait)))
    return tmp_path, entries


def test_arduino_wedged_write_leaves_dfu_and_retries_once(nicla, monkeypatch, capsys):
    root, entries = nicla
    ran = []
    fails = iter([True])

    def run(argv, **k):
        ran.append(argv)
        if "-D" in argv and next(fails, False):
            raise FlashError("flashing failed (DFU): exit 74")   # dfuDNBUSY / LIBUSB_ERROR_PIPE
    monkeypatch.setattr(fl.runner, "run", run)
    fl.flash_factory(str(root), board="ARDUINO_NICLA_VISION")
    leaves = [a for a in ran if a[-2:] == [":leave", "-R"]]
    assert len(leaves) == 1
    assert entries == [(True, 0), (True, fl._REBOOT_WAIT_S)]   # re-entered after the app is back
    assert len([a for a in ran if "-D" in a]) == 1 + 4           # the failed write, then all four
    assert "retrying once from a fresh entry" in capsys.readouterr().err


def test_arduino_wedged_twice_is_an_error(nicla, monkeypatch):
    root, _entries = nicla

    def run(argv, **k):
        if "-D" in argv:
            raise FlashError("flashing failed (DFU): exit 74")
    monkeypatch.setattr(fl.runner, "run", run)
    with pytest.raises(FlashError, match="exit 74"):
        fl.flash_factory(str(root), board="ARDUINO_NICLA_VISION")


def test_arduino_erase_enters_dfu_after_its_prechecks(tmp_path, monkeypatch):
    entered, ran = [], []
    monkeypatch.setattr(fl.tools, "find_dfu_util", lambda override, sdk_home: "DFU")
    monkeypatch.setattr(fl.history, "record", lambda *a, **k: None)
    monkeypatch.setattr(fl, "_arduino_enter", lambda *a, **k: entered.append(a[0]))
    monkeypatch.setattr(fl.runner, "run", lambda argv, **k: ran.append(argv))
    fl.flash_erase(str(tmp_path), board="ARDUINO_NICLA_VISION")
    assert entered == ["ARDUINO_NICLA_VISION"] and len(ran) == 1 and "-w" not in ran[0]


def test_cli_factory_provision_flag_reaches_the_rom_path(tmp_path, monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(fl, "flash_factory", lambda *a, **k: seen.update(k) or [])
    monkeypatch.setattr(fl, "provision_keys", lambda **k: [])
    assert main(["flash", "factory", str(tmp_path), "-b", "OPENMV_RT1060", "--provision"]) == 0
    assert seen["provision"] is True
    assert main(["flash", "factory", str(tmp_path), "-b", "OPENMV_RT1060"]) == 0
    assert seen["provision"] is False

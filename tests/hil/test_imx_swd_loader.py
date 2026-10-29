"""Reviving a DARK i.MX board over SWD (ci/hil/ota_cycle.py `imx_swd_loader`).

RT1060 node, 2026-10-28: a cancelled fleet run killed `flash factory` the second the board had
entered its SBL, leaving the firmware slot erased. The ROM then had nothing to boot and parked
with NO USB device at all -- the resident SBL only announces itself when a running app calls
machine.bootloader(), and this board's ROM serial downloader never enumerates (boot e-fuse). nRST,
core resets and the hub's sysfs port power toggle changed nothing; SWD still attached.

What brought it back, first try, was what `sdphost jump-address` does, driven by the J-Link: load
the CLI's bundled RAM flashloader at its IVT address, set PC to the IVT entry, go. Three seconds
later `15a2:0073` was on USB; after the two FlexSPI config commands the resident-SBL flash ops
worked through it unchanged, and the board booted the firmware they wrote.
"""

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_CIHIL = os.path.abspath(os.path.join(_HERE, "..", "..", "ci", "hil"))
sys.path.insert(0, _CIHIL)
os.environ.setdefault("WIFI_SSID", "")
os.environ.setdefault("WIFI_PASSWORD", "")

import ota_cycle  # noqa: E402  (ci/hil, added to sys.path above)
import recover  # noqa: E402

from openmv_ota.flash.targets import flash_config  # noqa: E402

_RT = "OPENMV_RT1060"
_RAW = flash_config(_RT).raw


def _rig(monkeypatch, *, enumerates=True, blhost_rc=0):
    """Capture every `sh` call; hand the J-Link commander script back by content (the file is
    unlinked right after the call). The loader 'enumerates' once the J-Link has run."""
    calls, state = [], {"jlink": False}

    def sh(cmd, **k):
        if cmd[0] == "pkill":
            return 0, ""
        if cmd[0] == "JLinkExe":
            calls.append(("jlink", cmd, open(cmd[cmd.index("-CommanderScript") + 1], "rb").read()))
            state["jlink"] = True
            return 0, ""
        calls.append(("blhost", cmd, b""))
        return blhost_rc, "Response status = %d" % blhost_rc
    monkeypatch.setattr(ota_cycle, "sh", sh)
    monkeypatch.setattr(ota_cycle, "_imx_sbl_present", lambda b: enumerates and state["jlink"])
    monkeypatch.setattr(ota_cycle, "_flash_raw", lambda b: _RAW if b == _RT else None)
    monkeypatch.setattr(ota_cycle.time, "sleep", lambda s: None)
    monkeypatch.setattr(ota_cycle, "_SWD_LOADER_ENUM_S", 0)
    monkeypatch.setattr(ota_cycle, "log", lambda m: None)
    monkeypatch.setitem(ota_cycle.CFG, "jlink", "JLinkExe")
    monkeypatch.setattr("openmv_ota.flash.tools.find_spsdk", lambda name, sdk: name)
    return calls


def test_usb_ids_spell_the_spsdk_form_the_sysfs_way():
    assert ota_cycle._usb_ids("0x15A2,0x0073") == ("15a2", "0073")
    assert ota_cycle._usb_ids("0x1FC9, 0x0135") == ("1fc9", "0135")


def test_sbl_present_scans_sysfs_for_the_boards_blhost_target(tmp_path, monkeypatch):
    for n, (v, p) in enumerate([("1366", "1020"), ("15a2", "0073")]):
        d = tmp_path / ("1-3.%d" % n)
        d.mkdir()
        (d / "idVendor").write_text(v + "\n")
        (d / "idProduct").write_text(p + "\n")
    (tmp_path / "1-9").mkdir()
    (tmp_path / "1-9" / "idVendor").write_text("1d6b\n")        # no idProduct: skipped, not fatal
    monkeypatch.setattr(ota_cycle, "_flash_raw", lambda b: _RAW)
    monkeypatch.setattr(ota_cycle.glob, "glob", lambda pat: [str(p) for p in tmp_path.glob("*/idVendor")])
    assert ota_cycle._imx_sbl_present(_RT) is True
    (tmp_path / "1-3.1" / "idProduct").write_text("1060\n")      # the runtime CDC is not the SBL
    assert ota_cycle._imx_sbl_present(_RT) is False


def test_sbl_present_is_false_for_a_board_without_an_imx_flash_config(monkeypatch):
    monkeypatch.setattr(ota_cycle, "_flash_raw", lambda b: {"backend": "dfu"})
    monkeypatch.setattr(ota_cycle.glob, "glob", lambda pat: pytest.fail("no scan for a DFU board"))
    assert ota_cycle._imx_sbl_present("OPENMV_N6") is False
    monkeypatch.setattr(ota_cycle, "_flash_raw", lambda b: None)
    assert ota_cycle._imx_sbl_present("NOPE") is False


def test_swd_loader_does_what_jump_address_does_then_configures_flexspi(monkeypatch):
    calls = _rig(monkeypatch)
    assert ota_cycle.imx_swd_loader(_RT) is True
    kinds = [c[0] for c in calls]
    assert kinds == ["jlink", "blhost", "blhost"], kinds
    _, cmd, script = calls[0]
    assert cmd[cmd.index("-device") + 1] == "MIMXRT1062xxx6A"
    assert cmd[cmd.index("-AutoConnect") + 1] == "1", "the loader needs a live core connect"
    lines = script.decode().splitlines()
    # reset + halt, load the bundled loader at its IVT address, PC = IVT entry (thumb bit clear), go
    assert lines[:2] == ["r", "halt"]
    assert lines[2].startswith("loadbin ") and lines[2].endswith("/sdphost_flash_loader.bin 0x20001C00")
    assert lines[3] == "SetPC 0x20002400"
    assert lines[4] == "g" and lines[-1] == "qc"
    # the SDP-loaded loader starts unconfigured: imx.plan's two config commands, nothing else
    fill, conf = calls[1][1], calls[2][1]
    assert fill[:3] == ["blhost", "-u", _RAW["blhost"]["usb"]]
    assert fill[fill.index("--") + 1:] == ["fill-memory", "0x2000", "4", "0xC0000008", "word"]
    assert conf[conf.index("--") + 1:] == ["configure-memory", "9", "0x2000"]
    for c in calls[1:]:
        assert not {"write-memory", "flash-erase-region"} & set(c[1]), "the harness never writes flash"


def test_swd_loader_gives_up_when_the_loader_never_enumerates(monkeypatch):
    calls = _rig(monkeypatch, enumerates=False)
    assert ota_cycle.imx_swd_loader(_RT) is False
    assert [c[0] for c in calls] == ["jlink"], "no blhost against a device that is not there"


def test_swd_loader_reports_a_failed_flexspi_config(monkeypatch):
    calls = _rig(monkeypatch, blhost_rc=1)
    assert ota_cycle.imx_swd_loader(_RT) is False
    assert [c[0] for c in calls] == ["jlink", "blhost"], "stops at the first failed command"


def test_swd_loader_is_a_no_op_off_the_imx(monkeypatch):
    monkeypatch.setattr(ota_cycle, "sh", lambda *a, **k: pytest.fail("nothing to drive"))
    monkeypatch.setattr(ota_cycle, "_flash_raw", lambda b: {"backend": "dfu"})
    assert ota_cycle.imx_swd_loader("OPENMV_N6") is False          # a J-Link, but not an i.MX
    monkeypatch.setitem(ota_cycle.BOARDS, "NO_JLINK", {"flash": "blhost_imx"})
    monkeypatch.setattr(ota_cycle, "_flash_raw", lambda b: _RAW)
    assert ota_cycle.imx_swd_loader("NO_JLINK") is False            # an i.MX, but no J-Link


def test_swd_loader_refuses_a_file_without_an_ivt(monkeypatch, tmp_path):
    bogus = tmp_path / "sdphost_flash_loader.bin"
    bogus.write_bytes(b"\xff" * 64)
    monkeypatch.setattr("openmv_ota.flash.flash._loader", lambda name, board: bogus)
    monkeypatch.setattr(ota_cycle, "sh", lambda *a, **k: pytest.fail("must not run J-Link"))
    monkeypatch.setattr(ota_cycle, "_flash_raw", lambda b: _RAW)
    monkeypatch.setattr(ota_cycle, "log", lambda m: None)
    assert ota_cycle.imx_swd_loader(_RT) is False


def test_kick_catch_never_pulses_a_board_whose_sbl_is_already_up(monkeypatch):
    """A loader lives in RAM: a reset pulse would land the board back in the dark ROM."""
    events = []
    monkeypatch.setattr(ota_cycle, "jlink_reset_pulse", lambda b: events.append("pulse"))
    monkeypatch.setattr(ota_cycle, "_imx_kick", lambda p: events.append("kick") or True)
    monkeypatch.setattr(ota_cycle.os.path, "exists", lambda p: False)
    monkeypatch.setattr(ota_cycle, "_imx_sbl_present", lambda b: True)
    monkeypatch.setattr(ota_cycle, "_IMX_KICK_EVERY_S", 0.05)
    monkeypatch.setattr(ota_cycle, "log", lambda m: None)
    code = ("import time\nprint(%r, flush=True); time.sleep(0.3)\nprint(%r, flush=True)\n"
            % (ota_cycle._IMX_ARMED, ota_cycle._IMX_CLAIMED))
    rc, out = ota_cycle.imx_kick_catch(_RT, [sys.executable, "-c", code], timeout=10)
    assert rc == 0 and events == []


def test_ensure_cdc_loads_the_flashloader_into_a_dark_imx_before_a_golden_flash(monkeypatch):
    """Absent after a pulse, no YKUSH, pre-flash: the loader goes in and ensure_cdc returns --
    there is no CDC to wait for in that state; the flash claims the loader --in-bootloader."""
    events = []
    monkeypatch.setattr(ota_cycle, "_cdc_answers", lambda: False)
    monkeypatch.setattr(ota_cycle, "sh", lambda cmd, **k: (0, ""))
    monkeypatch.setattr(ota_cycle, "_board_absent", lambda: True)
    monkeypatch.setattr(ota_cycle, "ykush_power_cycle", lambda b: False)
    monkeypatch.setattr(ota_cycle, "jlink_reset_pulse", lambda b: events.append("pulse"))
    monkeypatch.setattr(ota_cycle, "imx_swd_loader", lambda b: events.append("loader") or True)
    monkeypatch.setattr(ota_cycle, "recover_erase_romfs", lambda b: events.append("erase"))
    monkeypatch.setattr(ota_cycle.time, "sleep", lambda s: None)
    monkeypatch.setattr(ota_cycle, "log", lambda m: None)
    ota_cycle._ensure_cdc(_RT, allow_erase=True)
    assert events == ["pulse", "loader"], "one pulse first (it may be re-enumerating), then the loader"


def test_ensure_cdc_never_loads_the_flashloader_after_a_flash(monkeypatch):
    """Post-flash (allow_erase=False) the board belongs to the image just written; the loader is
    pre-flash only, like the erase."""
    events = []
    monkeypatch.setattr(ota_cycle, "_cdc_answers", lambda: False)
    monkeypatch.setattr(ota_cycle, "sh", lambda cmd, **k: (0, ""))
    monkeypatch.setattr(ota_cycle, "_board_absent", lambda: True)
    monkeypatch.setattr(ota_cycle, "ykush_power_cycle", lambda b: False)
    monkeypatch.setattr(ota_cycle, "jlink_reset_pulse", lambda b: events.append("pulse"))
    monkeypatch.setattr(ota_cycle, "imx_swd_loader", lambda b: pytest.fail("post-flash loader"))
    monkeypatch.setattr(ota_cycle.time, "sleep", lambda s: None)
    monkeypatch.setattr(ota_cycle, "log", lambda m: None)
    ota_cycle._ensure_cdc(_RT)
    assert events == ["pulse"] * 3


def test_recover_script_swd_loader_runs_without_a_probe(monkeypatch):
    calls = []

    def sh(cmd, **k):
        if "mpremote" in " ".join(map(str, cmd)):
            raise AssertionError("the REPL probe ran -- there is no port on a dark board")
        return 0, ""
    monkeypatch.setattr(recover.oc, "sh", sh)
    monkeypatch.setattr(recover.oc, "imx_swd_loader", lambda b: calls.append(b) or True)
    assert recover.main(["--board", _RT, "--swd-loader"]) == 0
    assert calls == [_RT]
    monkeypatch.setattr(recover.oc, "imx_swd_loader", lambda b: False)
    assert recover.main(["--board", _RT, "--swd-loader"]) == 1


def test_recover_script_escalates_to_the_loader_when_the_erase_cannot_reach_the_sbl(monkeypatch):
    events = []
    monkeypatch.setattr(recover.oc, "sh", lambda cmd, **k: (1, "no port"))   # every probe fails
    monkeypatch.setattr(recover.oc, "jlink_reset_pulse", lambda b: events.append("pulse"))
    monkeypatch.setattr(recover.oc, "recover_erase_romfs", lambda b: events.append("erase") or False)
    monkeypatch.setattr(recover.oc, "_board_absent", lambda: True)
    monkeypatch.setattr(recover.oc, "imx_swd_loader", lambda b: events.append("loader") or True)
    monkeypatch.setattr(recover.oc, "log", lambda m: None)
    assert recover.main(["--board", _RT]) == 0
    assert events == ["pulse", "erase", "loader"]
    monkeypatch.setattr(recover.oc, "imx_swd_loader", lambda b: False)
    assert recover.main(["--board", _RT]) == 1

"""`flash list`: enumerate connected boards and the state each is in (serial / dfu / imx)."""

from __future__ import annotations

from collections import namedtuple

import pytest

from openmv_ota.cli import main
from openmv_ota.flash import flash as fl
from openmv_ota.flash import inventory
from openmv_ota.flash.errors import FlashError

Port = namedtuple("Port", "device vid pid serial_number")


# --- the reverse index ----------------------------------------------------------------------

def test_index_maps_each_transport_and_collapses_the_shared_id():
    idx = inventory._index()
    assert idx[(0x37C5, 0x1204)] == ("OPENMV4", "running", "serial")          # runtime
    assert idx[(0x37C5, 0x9204)] == ("OPENMV4", "bootloader", "dfu")          # OpenMV DFU
    assert idx[(0x0483, 0xDF11)] == ("OpenMV STM32", "recovery", "dfu")       # shared system DFU
    assert idx[(0x0403, 0x6015)] == ("OPENMV_AE3", "recovery", "serial")      # AE3 SE-UART (FTDI)
    assert idx[(0x1A86, 0x55D3)] == ("OPENMV_AE3", "recovery", "serial")      # AE3 SE-UART (CH340)
    assert idx[(0x2341, 0x005B)] == ("ARDUINO_PORTENTA_H7", "running", "serial")   # arduino app
    assert idx[(0x2341, 0x035B)] == ("ARDUINO_PORTENTA_H7", "bootloader", "dfu")   # arduino DFU
    # retired Nanos are never indexed
    assert not any(label.startswith("ARDUINO_NANO") for label, _, _ in idx.values())


# --- serial scan ----------------------------------------------------------------------------

def test_serial_devices_reports_running_and_se_uart(monkeypatch):
    ports = [Port("/dev/ttyACM0", 0x37C5, 0x1204, "OMV42"),     # running OPENMV4
             Port("/dev/ttyUSB0", 0x0403, 0x6015, None),        # AE3 in SE-UART recovery
             Port("/dev/ttyACM9", 0x1234, 0x5678, "x")]         # not ours -> ignored
    monkeypatch.setattr(inventory.device, "_comports", lambda: ports)
    devs = inventory.serial_devices()
    assert (("OPENMV4", "running", "/dev/ttyACM0", "OMV42") in
            [(d.board, d.state, d.where, d.serial) for d in devs])
    ae3 = next(d for d in devs if d.board == "OPENMV_AE3")
    assert ae3.state == "recovery" and ae3.where == "/dev/ttyUSB0 (SE-UART)"
    assert len(devs) == 2


# --- dfu-util -l scan -----------------------------------------------------------------------

_DFU_L = """dfu-util 0.11

Found DFU: [0483:df11] ver=2200, devnum=12, cfg=1, intf=0, alt=0, name="@Internal Flash", serial="ST9"
Found DFU: [37c5:9204] ver=0200, devnum=15, cfg=1, intf=0, alt=0, name="@romfs", serial="OMV42"
Found DFU: [37c5:9204] ver=0200, devnum=15, cfg=1, intf=0, alt=1, name="@fw", serial="OMV42"
Found DFU: [1234:5678] ver=0100, devnum=9, cfg=1, intf=0, alt=0, name="@x", serial="UNKNOWN"
"""


def test_dfu_devices_dedups_alts_and_skips_unknown(monkeypatch):
    monkeypatch.setattr(inventory.runner, "output", lambda argv: _DFU_L)
    devs = inventory.dfu_devices("dfu-util")
    got = {(d.board, d.state, d.where, d.serial) for d in devs}
    assert got == {("OpenMV STM32", "recovery", "system DFU", "ST9"),   # the shared system DFU
                   ("OPENMV4", "bootloader", "DFU", "OMV42")}           # 2 alt lines -> 1 device


# --- imx (spsdk) scan -----------------------------------------------------------------------

def test_imx_devices_reports_sdp_rom_downloader(monkeypatch):
    # the ROM downloader (SDP) -- held in recovery, ready to flash
    monkeypatch.setattr(inventory.runner, "output", lambda argv: "FOUND 0x1FC9,0x0135\n")
    devs = inventory.imx_devices("python3")
    assert [(d.board, d.state, d.where) for d in devs] == [("OPENMV_RT1060", "recovery", "SDP ROM")]


def test_imx_devices_reports_the_loaded_flashloader(monkeypatch):
    # the RAM flashloader blhost talks to -- present only mid-flash (an interrupted flash)
    monkeypatch.setattr(inventory.runner, "output", lambda argv: "FOUND 0x15A2,0x0073\n")
    devs = inventory.imx_devices("python3")
    assert [(d.board, d.state, d.where) for d in devs] == [
        ("OPENMV_RT1060", "bootloader", "flashloader (mid-flash)")]


def test_imx_scans_both_the_downloader_and_the_flashloader(monkeypatch):
    seen = {}
    monkeypatch.setattr(inventory.runner, "output",
                        lambda argv: seen.setdefault("argv", argv) and "")
    assert inventory.imx_devices("python3") == []          # neither present
    flat = " ".join(seen["argv"])
    assert "0x1FC9,0x0135" in flat and "SdpUSBInterface" in flat        # SDP ROM
    assert "0x15A2,0x0073" in flat and "MbootUSBInterface" in flat      # the flashloader


# --- scan_devices composition + graceful degradation ----------------------------------------

@pytest.fixture
def stub_scanners(monkeypatch):
    d_serial = inventory.Device("OPENMV4", "running", "/dev/ttyACM0", "OMV42")
    d_dfu = inventory.Device("OpenMV STM32", "recovery", "system DFU", "ST9")
    d_imx = inventory.Device("OPENMV_RT1060", "recovery", "SDP ROM", None)
    monkeypatch.setattr(fl.inventory, "serial_devices", lambda: [d_serial])
    monkeypatch.setattr(fl.inventory, "dfu_devices", lambda tool: [d_dfu])
    monkeypatch.setattr(fl.inventory, "imx_devices", lambda py: [d_imx])
    monkeypatch.setattr(fl.tools, "find_dfu_util", lambda override, sdk_home: "DFU")
    monkeypatch.setattr(fl.tools, "find_spsdk", lambda name, sdk_home: "BLHOST")
    return d_serial, d_dfu, d_imx


def test_scan_devices_composes_all_three_sorted(stub_scanners):
    devs = fl.scan_devices()
    assert [d.board for d in devs] == ["OPENMV4", "OPENMV_RT1060", "OpenMV STM32"]  # sorted


def test_scan_devices_skips_dfu_when_dfu_util_missing(stub_scanners, monkeypatch, capsys):
    monkeypatch.setattr(fl.tools, "find_dfu_util",
                        lambda override, sdk_home: (_ for _ in ()).throw(FlashError("no dfu")))
    boards = [d.board for d in fl.scan_devices()]
    assert "OpenMV STM32" not in boards and "OPENMV4" in boards
    assert "skipping the DFU" in capsys.readouterr().err


def test_scan_devices_skips_imx_when_no_spsdk(stub_scanners, monkeypatch):
    monkeypatch.setattr(fl.tools, "find_spsdk",
                        lambda name, sdk_home: (_ for _ in ()).throw(FlashError("no spsdk")))
    assert "OPENMV_RT1060" not in [d.board for d in fl.scan_devices()]


# --- CLI ------------------------------------------------------------------------------------

def test_list_cli_human_output(monkeypatch, capsys):
    monkeypatch.setattr(fl, "scan_devices", lambda **k: [
        inventory.Device("OPENMV4", "running", "/dev/ttyACM0", "OMV42"),
        inventory.Device("OpenMV STM32", "recovery", "system DFU", None)])
    assert main(["flash", "list"]) == 0
    out = capsys.readouterr().out
    assert "OPENMV4" in out and "running" in out and "/dev/ttyACM0" in out and "OMV42" in out
    assert "system DFU" in out and "-" in out                # missing serial shown as '-'


def test_list_cli_json(monkeypatch, capsys):
    monkeypatch.setattr(fl, "scan_devices", lambda **k: [
        inventory.Device("OPENMV_RT1060", "recovery", "SDP ROM", None)])
    assert main(["flash", "list", "--json"]) == 0
    import json
    assert json.loads(capsys.readouterr().out) == [
        {"board": "OPENMV_RT1060", "state": "recovery", "where": "SDP ROM", "serial": None}]


def test_list_cli_empty(monkeypatch, capsys):
    monkeypatch.setattr(fl, "scan_devices", lambda **k: [])
    assert main(["flash", "list"]) == 0
    assert "no boards found" in capsys.readouterr().out


def test_list_cli_error_returns_exit_code(monkeypatch, capsys):
    monkeypatch.setattr(fl, "scan_devices",
                        lambda **k: (_ for _ in ()).throw(FlashError("boom")))
    assert main(["flash", "list"]) == 2
    assert "boom" in capsys.readouterr().err

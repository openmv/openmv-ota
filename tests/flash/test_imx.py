"""The i.MX RT1060 sdphost/blhost step planner (pure, hardware-free)."""

from __future__ import annotations

from openmv_ota.flash import imx
from openmv_ota.flash.targets import flash_config


def _raw():
    return flash_config("OPENMV_RT1060").raw


def _files(tmp_path, **sizes):
    files = {}
    for key, size in sizes.items():
        f = tmp_path / key
        f.write_bytes(b"x" * size)
        files[key] = f
    return files


def _plan(op, files):
    return imx.plan(op, _raw(), "sdphost", "blhost", "python3", files)


def test_aligned_rounds_up_to_sector():
    assert imx._aligned(1) == 0x1000
    assert imx._aligned(0x1000) == 0x1000
    assert imx._aligned(0x1001) == 0x2000


def test_firmware_plan_is_preamble_write_reset(tmp_path):
    files = _files(tmp_path, sdphost_loader=10, firmware=5000)
    steps = _plan("firmware", files)
    argvs = [s.argv for s in steps]
    assert argvs[0][:5] == ["sdphost", "-u", "0x1FC9,0x0135", "--", "write-file"]
    assert argvs[1][-1] == "0x20001C00" and argvs[1][4] == "jump-address"
    # the wait is a single scan-wait process (dfu-util -w equivalent), not a get-property poll
    assert argvs[2][:2] == ["python3", "-c"] and argvs[2][-2:] == ["0x15A2,0x0073", "30"]
    assert not any("get-property" in a for a in argvs)
    # FlexSPI config, then erase(rounded)+write firmware, then reset -- no FCB/SBL/efuse
    assert ["fill-memory", "0x2000", "4", "0xC0000008", "word"] == argvs[3][4:]
    assert argvs[5][-3:] == ["flash-erase-region", "0x60040000", "0x2000"]   # 5000 -> 0x2000
    assert argvs[6][4:6] == ["write-memory", "0x60040000"]
    assert argvs[-1][-1] == "reset"
    assert not any("efuse-program-once" in a for a in argvs)


def test_wait_argv_runs_the_spsdk_scan_in_one_process():
    argv = imx._wait_argv("python3", "0x15A2,0x0073")
    assert argv[:2] == ["python3", "-c"] and "scan(device_id=dev)" in argv[2]
    assert argv[3:] == ["spsdk.mboot.interfaces.usb", "MbootUSBInterface", "0x15A2,0x0073", "30"]
    sdp = imx._wait_argv("python3", "0x1FC9,0x0135", sdp=True)
    assert sdp[3:] == ["spsdk.sdp.interfaces.usb", "SdpUSBInterface", "0x1FC9,0x0135", "120"]


def test_bootloader_plan_waits_for_rom_then_writes_fcb_and_sbl(tmp_path):
    files = _files(tmp_path, sdphost_loader=10, blhost_loader=2000)
    steps = _plan("bootloader", files)
    argvs = [s.argv for s in steps]
    # the ROM (SDP) wait comes first (manual SBL entry), then sdphost
    assert argvs[0][3:5] == ["spsdk.sdp.interfaces.usb", "SdpUSBInterface"]
    assert argvs[1][4] == "write-file"
    flat = " ".join(" ".join(a) for a in argvs)
    assert "flash-erase-region 0x60000000 0x1000" in flat            # FCB
    assert "write-memory 0x60001000" in flat                         # secure bootloader
    assert "0x60040000" not in flat and "efuse-program-once" not in flat   # no firmware/efuse
    assert steps[-1].argv[-1] == "reset"


def test_romfs_plan_targets_romfs_region(tmp_path):
    files = _files(tmp_path, sdphost_loader=10, romfs=9000)
    argvs = [s.argv for s in _plan("romfs", files)]
    assert argvs[5][-3:] == ["flash-erase-region", "0x60800000", "0x3000"]   # 9000 -> 0x3000
    assert argvs[6][4:6] == ["write-memory", "0x60800000"]


def test_factory_plan_writes_fcb_sbl_firmware_romfs_efuse(tmp_path):
    files = _files(tmp_path, sdphost_loader=10, blhost_loader=2000, firmware=5000, romfs=9000)
    steps = _plan("factory", files)
    flat = " ".join(" ".join(s.argv) for s in steps)
    # FCB block, then SBL, firmware, romfs writes, then efuse + reset
    assert "flash-erase-region 0x60000000 0x1000" in flat            # FCB
    assert "0xF000000F" in flat                                      # FCB config value
    assert "write-memory 0x60001000" in flat                         # secure bootloader
    assert "write-memory 0x60040000" in flat                         # firmware
    assert "write-memory 0x60800000" in flat                         # romfs
    assert "efuse-program-once 0x06 00000010" in flat
    assert steps[-1].argv[-1] == "reset"


def test_blhost_timeout_only_on_erase(tmp_path):
    files = _files(tmp_path, sdphost_loader=10, firmware=5000)
    steps = _plan("firmware", files)
    erase = next(s for s in steps if "flash-erase-region" in s.argv)
    assert "-t" in erase.argv and "120000" in erase.argv
    reset = next(s for s in steps if s.argv[-1] == "reset")
    assert "-t" not in reset.argv

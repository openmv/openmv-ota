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


def test_firmware_plan_uses_resident_sbl_no_sdphost_no_config(tmp_path):
    # the everyday update path: drive the resident SBL (machine.bootloader -> blhost), which is
    # already FlexSPI-configured from the FCB -- so no sdphost flashloader load and no config writes
    files = _files(tmp_path, firmware=5000)
    steps = _plan("firmware", files)
    argvs = [s.argv for s in steps]
    assert not any(a[0] == "sdphost" for a in argvs)
    assert not any("fill-memory" in a or "configure-memory" in a for a in argvs)
    assert not any("get-property" in a for a in argvs)
    # NO wait step in the plan -- flash.py's catcher enters+claims the resident SBL first. The plan
    # is just erase(rounded)+write, then reset.
    assert argvs[0][-3:] == ["flash-erase-region", "0x60040000", "0x2000"]   # 5000 -> 0x2000
    assert argvs[1][4:6] == ["write-memory", "0x60040000"]
    assert argvs[-1][-1] == "reset"
    flat = " ".join(" ".join(a) for a in argvs)
    assert "efuse-program-once" not in flat and "0x60000000" not in flat     # no FCB/SBL/efuse


def test_wait_argv_runs_the_spsdk_scan_in_one_process():
    argv = imx._wait_argv("python3", "0x15A2,0x0073")
    assert argv[:2] == ["python3", "-c"] and "scan(device_id=dev)" in argv[2]
    assert argv[3:] == ["spsdk.mboot.interfaces.usb", "MbootUSBInterface", "0x15A2,0x0073", "30"]
    sdp = imx._wait_argv("python3", "0x1FC9,0x0135", sdp=True)
    assert sdp[3:] == ["spsdk.sdp.interfaces.usb", "SdpUSBInterface", "0x1FC9,0x0135", "120"]


def test_catcher_argv_arms_the_claim_script():
    # the resident-SBL catcher: arm it, wait for READY, THEN reset -> it CLAIMS the SBL (holds it
    # against the idle timeout). Direct UsbDevice.scan (no spsdk device-DB FileLock stall).
    argv = imx.catcher_argv("python3", "0x15A2,0x0073")
    assert argv[:2] == ["python3", "-c"]
    assert "print('READY'" in argv[2] and "get_property(1)" in argv[2]
    assert "UsbDevice.scan(device_id=dev)" in argv[2]
    assert argv[3:] == ["claim", "0x15A2,0x0073", "30", str(imx.SBL_EXPECTED_VERSION)]
    assert imx.catcher_argv("python3", "0x15A2,0x0073", mode="wait", timeout_s=5)[3:5] == ["wait", "0x15A2,0x0073"]


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


def test_romfs_plan_targets_romfs_region_via_resident_sbl(tmp_path):
    files = _files(tmp_path, romfs=9000)
    argvs = [s.argv for s in _plan("romfs", files)]
    assert argvs[0][-3:] == ["flash-erase-region", "0x60800000", "0x3000"]   # 9000 -> 0x3000
    assert argvs[1][4:6] == ["write-memory", "0x60800000"]
    assert argvs[-1][-1] == "reset"
    assert not any(a[0] == "sdphost" or "fill-memory" in a for a in argvs)


def test_erase_plan_wipes_disk_mbr_via_resident_sbl():
    argvs = [s.argv for s in _plan("erase", {})]     # erase needs no artifact files
    assert argvs[0][-3:] == ["flash-erase-region", "0x60400000", "0x1000"]   # the user-disk MBR
    assert argvs[-1][-1] == "reset"
    assert not any(a[0] == "sdphost" or "write-memory" in a for a in argvs)


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

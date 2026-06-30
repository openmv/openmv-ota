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


def test_aligned_rounds_up_to_sector():
    assert imx._aligned(1) == 0x1000
    assert imx._aligned(0x1000) == 0x1000
    assert imx._aligned(0x1001) == 0x2000


def test_firmware_plan_is_preamble_write_reset(tmp_path):
    files = _files(tmp_path, sdphost_loader=10, firmware=5000)
    steps = imx.plan("firmware", _raw(), "sdphost", "blhost", files)
    argvs = [s.argv for s in steps]
    assert argvs[0][:5] == ["sdphost", "-u", "0x1FC9,0x0135", "--", "write-file"]
    assert argvs[1][-1] == "0x20001C00" and argvs[1][4] == "jump-address"
    assert steps[2].probe is True and argvs[2][-2:] == ["get-property", "1"]
    # FlexSPI config, then erase(rounded)+write firmware, then reset -- no FCB/SBL/efuse
    assert ["fill-memory", "0x2000", "4", "0xC0000008", "word"] == argvs[3][4:]
    assert argvs[5][-3:] == ["flash-erase-region", "0x60040000", "0x2000"]   # 5000 -> 0x2000
    assert argvs[6][4:6] == ["write-memory", "0x60040000"]
    assert argvs[-1][-1] == "reset"
    assert not any("efuse-program-once" in a for a in argvs)


def test_romfs_plan_targets_romfs_region(tmp_path):
    files = _files(tmp_path, sdphost_loader=10, romfs=9000)
    argvs = [s.argv for s in imx.plan("romfs", _raw(), "sdphost", "blhost", files)]
    assert argvs[5][-3:] == ["flash-erase-region", "0x60800000", "0x3000"]   # 9000 -> 0x3000
    assert argvs[6][4:6] == ["write-memory", "0x60800000"]


def test_factory_plan_writes_fcb_sbl_firmware_romfs_efuse(tmp_path):
    files = _files(tmp_path, sdphost_loader=10, blhost_loader=2000, firmware=5000, romfs=9000)
    steps = imx.plan("factory", _raw(), "sdphost", "blhost", files)
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
    steps = imx.plan("firmware", _raw(), "sdphost", "blhost", files)
    erase = next(s for s in steps if "flash-erase-region" in s.argv)
    assert "-t" in erase.argv and "120000" in erase.argv
    getp = next(s for s in steps if "get-property" in s.argv)
    assert "-t" not in getp.argv


def test_imxstep_summary_is_label(tmp_path):
    files = _files(tmp_path, sdphost_loader=10, firmware=5000)
    s = imx.plan("firmware", _raw(), "sdphost", "blhost", files)[0]
    assert s.summary == s.label

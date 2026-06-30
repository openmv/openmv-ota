"""AE3 bootloader via the Alif Security Toolkit: update-system-package -> replug -> MRAM write."""

from __future__ import annotations

from collections import namedtuple
from pathlib import Path

import pytest

from openmv_ota.flash import alif
from openmv_ota.flash import flash as fl
from openmv_ota.flash import tools
from openmv_ota.flash.errors import FlashError

Port = namedtuple("Port", "device vid pid serial_number")
VARIANTS = [{"name": "SBL", "bridge": "0403:6015", "cfg_part": "AE302F80F55D5AE"},
            {"name": "SBL2", "bridge": "1a86:55d3", "cfg_part": "AE302F80F55D5LE"}]


# --- pure planner ---------------------------------------------------------------------------

def test_find_se_uart_none_one_and_multiple():
    with pytest.raises(FlashError, match="no AE3 in SE-UART"):
        alif.find_se_uart(VARIANTS, [])
    se = alif.find_se_uart(VARIANTS, [Port("/dev/ttyUSB1", 0x1A86, 0x55D3, None)])
    assert se.port == "/dev/ttyUSB1" and se.cfg_part == "AE302F80F55D5LE" and se.name == "SBL2"
    with pytest.raises(FlashError, match="multiple AE3"):
        alif.find_se_uart(VARIANTS, [Port("/d0", 0x0403, 0x6015, None),
                                     Port("/d1", 0x1A86, 0x55D3, None)])


def test_argv_builders_and_images_string():
    se = alif.SeUart("/dev/ttyUSB0", "AE302F80F55D5AE", "SBL")
    usp = alif.update_system_package_argv("py", "/TK", se, "B4")
    assert usp == ["py", "/TK/updateSystemPackage.py", "--port", "/dev/ttyUSB0",
                   "--cfg-part", "AE302F80F55D5AE", "--cfg-rev", "B4"]
    images = alif.images_arg(
        [{"file": "bootloader.bin", "addr": "0x80000000"},
         {"file": "firmware_pad.toc", "addr": "0x8057e000"}],
        {"bootloader.bin": Path("/o/b.bin"), "firmware_pad.toc": Path("/o/p.toc")})
    assert images == "/o/b.bin 0x80000000 /o/p.toc 0x8057e000"
    w = alif.write_bootloader_argv("py", "/TK", se, "B4", images)
    assert w[1] == "/TK/app-write-mram.py" and "--pad" in w
    assert w[-2:] == ["--images", images] and "--erase" not in w


def test_find_alif_toolkit_prefers_board_path_then_micropython(tmp_path):
    with pytest.raises(FlashError, match="not found"):
        tools.find_alif_toolkit(tmp_path, "tools/alif/toolkit")
    tk = tmp_path / "tools/alif/toolkit"
    tk.mkdir(parents=True)
    (tk / "app-write-mram.py").write_text("")
    assert tools.find_alif_toolkit(tmp_path, "tools/alif/toolkit") == str(tk)

    p2 = tmp_path / "p2"
    mp = p2 / "lib/micropython/lib/alif-security-toolkit/toolkit"
    mp.mkdir(parents=True)
    (mp / "app-write-mram.py").write_text("")
    assert tools.find_alif_toolkit(p2, "tools/alif/toolkit") == str(mp)


# --- orchestration --------------------------------------------------------------------------

@pytest.fixture
def ae3_project(tmp_path, monkeypatch):
    out = tmp_path / "build"
    out.mkdir()
    (out / "OPENMV_AE3-bootloader.bin").write_bytes(b"BL")
    (out / "OPENMV_AE3-firmware_pad.toc").write_bytes(b"TOC")
    ran: list = []
    monkeypatch.setattr(fl.runner, "run", lambda argv, **k: ran.append(argv))
    monkeypatch.setattr(fl.tools, "find_alif_toolkit", lambda project, rel: "/TK")
    monkeypatch.setattr(fl.history, "record", lambda *a, **k: None)
    return tmp_path, ran


def test_ae3_updates_system_package_then_replug_then_writes(ae3_project, monkeypatch, capsys):
    root, ran = ae3_project
    monkeypatch.setattr(fl.device, "_comports",
                        lambda: [Port("/dev/ttyUSB0", 0x0403, 0x6015, None)])
    prompts: list = []
    monkeypatch.setattr("builtins.input", lambda *a: prompts.append(1) or "")

    steps = fl.flash_bootloader(str(root), board="OPENMV_AE3")

    # update the system package first (coupled to the bootloader), on the detected port + part
    assert ran[0][1].endswith("updateSystemPackage.py")
    assert ran[0][ran[0].index("--port") + 1] == "/dev/ttyUSB0"
    assert ran[0][ran[0].index("--cfg-part") + 1] == "AE302F80F55D5AE"
    # the operator is told to power-cycle between the two steps (mandatory on a virgin part)
    assert prompts == [1]
    # then the SBL + padded TOC are written to MRAM -- never the APP mass-erase
    assert ran[1][1].endswith("app-write-mram.py") and "--pad" in ran[1] and "--erase" not in ran[1]
    img = ran[1][ran[1].index("--images") + 1]
    assert "OPENMV_AE3-bootloader.bin 0x80000000" in img
    assert "OPENMV_AE3-firmware_pad.toc 0x8057e000" in img
    assert [s.label for s in steps] == ["update system package", "write bootloader"]
    assert "SE-UART maintenance mode" in capsys.readouterr().err


def test_ae3_sbl2_variant_uses_ch340_part(ae3_project, monkeypatch):
    root, ran = ae3_project
    monkeypatch.setattr(fl.device, "_comports",
                        lambda: [Port("/dev/ttyUSB9", 0x1A86, 0x55D3, None)])
    monkeypatch.setattr("builtins.input", lambda *a: "")
    fl.flash_bootloader(str(root), board="OPENMV_AE3")
    assert ran[0][ran[0].index("--cfg-part") + 1] == "AE302F80F55D5LE"
    assert ran[1][ran[1].index("--port") + 1] == "/dev/ttyUSB9"


def test_ae3_dry_run_shows_both_commands_needs_no_hardware(tmp_path, monkeypatch):
    out = tmp_path / "build"
    out.mkdir()
    (out / "OPENMV_AE3-bootloader.bin").write_bytes(b"BL")
    (out / "OPENMV_AE3-firmware_pad.toc").write_bytes(b"TOC")
    ran: list = []
    monkeypatch.setattr(fl.runner, "run", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(fl.tools, "find_alif_toolkit",       # dry-run tolerates a missing toolkit
                        lambda *a: (_ for _ in ()).throw(FlashError("no toolkit")))
    called: list = []
    monkeypatch.setattr("builtins.input", lambda *a: called.append(1))

    steps = fl.flash_bootloader(str(tmp_path), board="OPENMV_AE3", dry_run=True)

    assert ran == [] and called == []           # nothing runs, no operator prompt
    assert steps[0].argv[1].endswith("updateSystemPackage.py")
    assert steps[1].argv[1].endswith("app-write-mram.py")
    assert "<se-uart-port>" in steps[0].argv     # placeholder port (no hardware probed)


def test_ae3_missing_artifact_fails_fast(tmp_path, monkeypatch):
    (tmp_path / "build").mkdir()
    monkeypatch.setattr(fl.tools, "find_alif_toolkit", lambda *a: "/TK")
    monkeypatch.setattr(fl.device, "_comports",
                        lambda: [Port("/dev/ttyUSB0", 0x0403, 0x6015, None)])
    with pytest.raises(FlashError, match="OPENMV_AE3-bootloader.bin"):
        fl.flash_bootloader(str(tmp_path), board="OPENMV_AE3")

"""Tests for the read-only resolvers (macros, firmware, micropython, sdk, board)."""

from __future__ import annotations

from pathlib import Path

import pytest

from openmv_ota.project.errors import ProjectError
from openmv_ota.project.resolve import board as board_res
from openmv_ota.project.resolve import firmware as fw_res
from openmv_ota.project.resolve import micropython as mp_res
from openmv_ota.project.resolve import sdk as sdk_res
from openmv_ota.project.resolve.macros import parse_defines


# --- macros -----------------------------------------------------------------

def test_parse_defines_strips_parens_quotes_comments():
    text = (
        "#define A (5)\n"
        '#define B "N6"   // a comment\n'
        "#define C 0x10 /* block */\n"
        "#  define D   7\n"
        "not a define\n"
    )
    got = parse_defines(text, ["A", "B", "C", "D", "MISSING"])
    assert got == {"A": "5", "B": "N6", "C": "0x10", "D": "7"}


# --- firmware ---------------------------------------------------------------

def test_firmware_version(make_firmware):
    repo = make_firmware()
    v = fw_res.resolve_firmware_version(repo)
    assert (v.major, v.minor, v.patch) == (5, 0, 0)
    assert v.string == "5.0.0"
    assert v.code == (5 << 24)


def test_firmware_missing_header(make_firmware):
    repo = make_firmware(omit=("omv_protocol",))
    with pytest.raises(ProjectError, match="cannot read"):
        fw_res.resolve_firmware_version(repo)


def test_firmware_missing_macros(tmp_path):
    hdr = tmp_path / "protocol" / "omv_protocol.h"
    hdr.parent.mkdir(parents=True)
    hdr.write_text("#define OMV_FIRMWARE_VERSION_MAJOR (5)\n")  # minor/patch absent
    with pytest.raises(ProjectError, match="could not find"):
        fw_res.resolve_firmware_version(tmp_path)


def test_firmware_non_integer(tmp_path):
    hdr = tmp_path / "protocol" / "omv_protocol.h"
    hdr.parent.mkdir(parents=True)
    hdr.write_text(
        "#define OMV_FIRMWARE_VERSION_MAJOR (x)\n"
        "#define OMV_FIRMWARE_VERSION_MINOR (0)\n"
        "#define OMV_FIRMWARE_VERSION_PATCH (0)\n"
    )
    with pytest.raises(ProjectError, match="non-integer"):
        fw_res.resolve_firmware_version(tmp_path)


# --- micropython ------------------------------------------------------------

def test_micropython(make_firmware):
    repo = make_firmware()
    mp = mp_res.resolve_micropython(repo)
    assert mp.version == "1.28.0"
    assert mp.prerelease is False
    assert (mp.mpy_abi_version, mp.mpy_sub_version) == (6, 3)


def test_micropython_uninitialized(make_firmware):
    repo = make_firmware(omit=("mpconfig",))
    with pytest.raises(ProjectError, match="submodule not initialized"):
        mp_res.resolve_micropython(repo)


def test_micropython_missing_version_macro(tmp_path):
    py = tmp_path / "lib" / "micropython" / "py"
    py.mkdir(parents=True)
    (py / "mpconfig.h").write_text("#define MICROPY_VERSION_MAJOR 1\n")
    (py / "persistentcode.h").write_text("#define MPY_VERSION 6\n#define MPY_SUB_VERSION 3\n")
    with pytest.raises(ProjectError, match="could not find"):
        mp_res.resolve_micropython(tmp_path)


def test_micropython_missing_mpy_macros(tmp_path):
    py = tmp_path / "lib" / "micropython" / "py"
    py.mkdir(parents=True)
    (py / "mpconfig.h").write_text(
        "#define MICROPY_VERSION_MAJOR 1\n#define MICROPY_VERSION_MINOR 28\n"
        "#define MICROPY_VERSION_MICRO 0\n#define MICROPY_VERSION_PRERELEASE 0\n"
    )
    (py / "persistentcode.h").write_text("#define MPY_VERSION 6\n")  # no SUB
    with pytest.raises(ProjectError, match="MPY_VERSION/MPY_SUB_VERSION"):
        mp_res.resolve_micropython(tmp_path)


def test_micropython_non_integer(tmp_path):
    py = tmp_path / "lib" / "micropython" / "py"
    py.mkdir(parents=True)
    (py / "mpconfig.h").write_text(
        "#define MICROPY_VERSION_MAJOR x\n#define MICROPY_VERSION_MINOR 28\n"
        "#define MICROPY_VERSION_MICRO 0\n#define MICROPY_VERSION_PRERELEASE 0\n"
    )
    (py / "persistentcode.h").write_text("#define MPY_VERSION 6\n#define MPY_SUB_VERSION 3\n")
    with pytest.raises(ProjectError, match="non-integer"):
        mp_res.resolve_micropython(tmp_path)


# --- sdk --------------------------------------------------------------------

def test_read_sdk_version(make_firmware):
    assert sdk_res.read_sdk_version(make_firmware()) == "1.6.0"


def test_read_sdk_version_missing(make_firmware):
    with pytest.raises(ProjectError, match="not found"):
        sdk_res.read_sdk_version(make_firmware(omit=("sdk_version",)))


def test_read_sdk_version_empty(tmp_path):
    (tmp_path / "SDK_VERSION").write_text("   ")
    with pytest.raises(ProjectError, match="empty"):
        sdk_res.read_sdk_version(tmp_path)


def test_default_sdk_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert sdk_res.default_sdk_home("1.6.0") == tmp_path / "openmv-sdk-1.6.0"


def test_resolve_sdk_installed(make_firmware, make_sdk):
    repo = make_firmware()
    home = make_sdk()
    info = sdk_res.resolve_sdk(repo, home)
    assert info.installed and info.stamp_matches and info.declared_version == "1.6.0"


def test_resolve_sdk_not_installed(make_firmware, tmp_path):
    info = sdk_res.resolve_sdk(make_firmware(), tmp_path / "nope")
    assert not info.installed and not info.stamp_matches and info.stamp_version is None


def test_resolve_sdk_stamp_mismatch(make_firmware, make_sdk):
    home = make_sdk(stamp="9.9.9")
    info = sdk_res.resolve_sdk(make_firmware(), home)
    assert info.installed and not info.stamp_matches


def test_resolve_vela(make_sdk):
    info = sdk_res.resolve_vela(make_sdk(with_bins=True))
    assert info.found and info.version == "5.0.0" and info.path.endswith("/bin/vela")


def test_resolve_vela_windows_layout(make_sdk):
    info = sdk_res.resolve_vela(make_sdk(with_bins=True, windows_layout=True))
    assert info.found and info.path.endswith("Scripts/vela.exe")


def test_resolve_vela_absent(make_sdk):
    info = sdk_res.resolve_vela(make_sdk(vela=None))
    assert not info.found and info.version is None and info.path is None


def test_resolve_vela_no_binary(make_sdk):
    info = sdk_res.resolve_vela(make_sdk(with_bins=False))
    assert info.found and info.path is None


def test_resolve_stedgeai(make_sdk):
    info = sdk_res.resolve_stedgeai(make_sdk(with_bins=True))
    assert info.found and info.version == "4.0" and info.path.endswith("/linux/stedgeai")


def test_resolve_stedgeai_absent(make_sdk):
    info = sdk_res.resolve_stedgeai(make_sdk(stedgeai=None))
    assert not info.found and info.version is None


def test_resolve_stedgeai_picks_highest(make_sdk):
    home = make_sdk()
    (home / "stedgeai" / "stedgeai0500").mkdir()
    (home / "stedgeai" / "stedgeai-old").mkdir()  # starts with stedgeai but no \d{4}
    info = sdk_res.resolve_stedgeai(home)
    assert info.version == "5.0"


def test_resolve_mpy_cross(make_firmware, make_sdk):
    repo = make_firmware()
    mp = mp_res.resolve_micropython(repo)
    info = sdk_res.resolve_mpy_cross(repo, mp)
    assert info.found and info.version == "1.28.0" and info.path is None
    # With a built binary present:
    built = repo / "lib" / "micropython" / "mpy-cross" / "build"
    built.mkdir(parents=True)
    (built / "mpy-cross").write_text("")
    info2 = sdk_res.resolve_mpy_cross(repo, mp)
    assert info2.path.endswith("build/mpy-cross")


# --- board ------------------------------------------------------------------

def test_board_geometry_from_firmware(make_firmware):
    repo = make_firmware()
    rb, warnings = board_res.resolve_board(repo, "OPENMV_N6")
    assert rb.geometry_source == "firmware"
    assert rb.partition_size == 0x01800000
    assert rb.front_size == (0x01800000 // 2)
    assert rb.board_type == "N6"
    assert rb.npu == "stedgeai"
    # The full compiler config (args + file refs) is carried for the compile layer.
    assert rb.npu_config["type"] == "stedgeai"
    assert any("--target" in a for a in rb.npu_config["args"])
    assert warnings == []


def test_board_geometry_conditional_falls_back(make_firmware):
    repo = make_firmware()
    rb, warnings = board_res.resolve_board(repo, "OPENMV_AE3")
    assert rb.geometry_source == "bundled"
    assert any("conditional" in w for w in warnings)


def test_board_geometry_override(make_firmware):
    repo = make_firmware()
    rb, _ = board_res.resolve_board(repo, "OPENMV_N6", override={"partition_size": 4096, "board_id": 7})
    assert rb.geometry_source == "override"
    assert rb.partition_size == 4096 and rb.board_id == 7


def test_board_firmware_differs_from_bundled_warns(make_firmware):
    repo = make_firmware()
    # Rewrite N6 header with a non-bundled size so the firmware!=bundled warning fires.
    hdr = repo / "boards" / "OPENMV_N6" / "board_config.h"
    hdr.write_text('#define OMV_BOARD_TYPE "N6"\n#define OMV_ROMFS_PART0_LENGTH 0x02000000\n')
    rb, warnings = board_res.resolve_board(repo, "OPENMV_N6")
    assert rb.geometry_source == "firmware" and rb.partition_size == 0x02000000
    assert any("differs from bundled" in w for w in warnings)


def test_board_unknown(make_firmware):
    with pytest.raises(ProjectError, match="unknown board"):
        board_res.resolve_board(make_firmware(), "NOPE")


def test_board_bad_partition_index(make_firmware):
    with pytest.raises(ProjectError):
        board_res.resolve_board(make_firmware(), "OPENMV_N6", partition_index=9)


def test_board_no_firmware_header_uses_bundled(make_firmware):
    repo = make_firmware()
    # A board with no board_config.h in the firmware tree falls back to bundled.
    rb, _ = board_res.resolve_board(repo, "OPENMV4")
    assert rb.geometry_source == "bundled"
    assert rb.board_type is None
    assert rb.npu is None and rb.npu_config is None  # board without an NPU


def test_board_unparseable_firmware_token_falls_back(make_firmware):
    repo = make_firmware()
    hdr = repo / "boards" / "OPENMV_N6" / "board_config.h"
    hdr.write_text('#define OMV_BOARD_TYPE "N6"\n#define OMV_ROMFS_PART0_LENGTH SOME_MACRO\n')
    rb, _ = board_res.resolve_board(repo, "OPENMV_N6")
    assert rb.geometry_source == "bundled"  # non-integer token skipped


def test_board_no_size_anywhere_raises(make_firmware):
    repo = make_firmware()
    # A board whose bundled partition size is 0 and has no firmware header.
    with pytest.raises(ProjectError, match="no partition size"):
        board_res.resolve_board(repo, "ARDUINO_NANO_33_BLE_SENSE")

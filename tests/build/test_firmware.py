"""Tests for ``openmv-ota build firmware`` (the make invocation is mocked -- no
ARM toolchain in CI)."""

from __future__ import annotations

import subprocess
import types
from pathlib import Path

import pytest

from openmv_ota.build import firmware as fw
from openmv_ota.build.errors import BuildError


def _fake_make(artifacts):
    """A drop-in for ``fw._run_make`` that records calls and, on the build call
    (not ``clean``), drops ``artifacts`` (paths relative to ``build/<TARGET>``)."""
    calls: list[list[str]] = []

    def fake(repo, args):
        calls.append(list(args))
        target = next(a.split("=", 1)[1] for a in args if a.startswith("TARGET="))
        if "clean" in args:
            return
        bdir = Path(repo) / "build" / target
        for rel in artifacts:
            f = bdir / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"FW")

    fake.calls = calls
    return fake


_COMMON_REL = "lib/micropython/extmod/mbedtls/mbedtls_config_common.h"
_PORT_REL = "lib/micropython/ports/stm32/mbedtls/mbedtls_config_port.h"


# --- PEM-enable: a patched COPY of the per-port mbedtls config (source untouched) -----

def _fake_fw(tmp_path, *, port="stm32", pem_in_common=False, port_cfg=True):
    repo = tmp_path / "fw"
    common = repo / _COMMON_REL
    common.parent.mkdir(parents=True)
    common.write_text("#define MBEDTLS_X509_USE_C\n"
                      + ("#define MBEDTLS_PEM_PARSE_C\n" if pem_in_common else ""))
    bd = repo / "boards" / "OPENMV_N6"
    bd.mkdir(parents=True)
    (bd / "board_config.mk").write_text("PORT=%s\n" % port)
    if port_cfg:
        pc = repo / "lib" / "micropython" / "ports" / port / "mbedtls"
        pc.mkdir(parents=True)
        (pc / "mbedtls_config_port.h").write_text(
            '#include <time.h>\n#include "extmod/mbedtls/mbedtls_config_common.h"\n#endif\n')
    return repo


def test_board_port_from_board_config_mk(tmp_path):
    repo = _fake_fw(tmp_path, port="alif")
    assert fw._board_port(repo, "OPENMV_N6") == "alif"
    assert fw._board_port(repo, "NOPE") is None        # no boards/NOPE/board_config.mk


def test_board_port_none_without_port_line(tmp_path):
    repo = tmp_path / "fw"
    (repo / "boards" / "B").mkdir(parents=True)
    (repo / "boards" / "B" / "board_config.mk").write_text("FOO=bar\n")
    assert fw._board_port(repo, "B") is None


def test_pem_config_arg_copies_and_patches_port_config(tmp_path):
    repo = _fake_fw(tmp_path)
    tmp = tmp_path / "t"
    tmp.mkdir()
    arg = fw._pem_config_arg(repo, tmp, "OPENMV_N6")
    assert arg is not None and arg.startswith('MBEDTLS_CONFIG_FILE=\\"') and arg.endswith('\\"')
    dst = tmp / "mbedtls_config_port.h"
    txt = dst.read_text()
    assert "MBEDTLS_BASE64_C" in txt and "MBEDTLS_PEM_PARSE_C" in txt
    assert '#include "extmod/mbedtls/mbedtls_config_common.h"' in txt   # still chains to common
    assert dst.as_posix() in arg
    assert "X509_USE_C" in (repo / _COMMON_REL).read_text()             # source untouched


def test_pem_config_arg_none_when_already_enabled(tmp_path):
    tmp = tmp_path / "t"
    tmp.mkdir()
    assert fw._pem_config_arg(_fake_fw(tmp_path, pem_in_common=True), tmp, "OPENMV_N6") is None


def test_pem_config_arg_enables_when_common_unreadable(tmp_path):
    repo = _fake_fw(tmp_path)
    (repo / _COMMON_REL).unlink()                  # can't detect -> enable to be safe
    tmp = tmp_path / "t"
    tmp.mkdir()
    assert fw._pem_config_arg(repo, tmp, "OPENMV_N6") is not None


def test_pem_config_arg_appends_when_no_common_include(tmp_path):
    repo = _fake_fw(tmp_path)
    (repo / _PORT_REL).write_text("#define X\n")    # port config without the include anchor
    tmp = tmp_path / "t"
    tmp.mkdir()
    fw._pem_config_arg(repo, tmp, "OPENMV_N6")
    assert "MBEDTLS_PEM_PARSE_C" in (tmp / "mbedtls_config_port.h").read_text()


def test_pem_config_arg_warns_when_port_config_missing(tmp_path, capsys):
    repo = _fake_fw(tmp_path, port_cfg=False)
    tmp = tmp_path / "t"
    tmp.mkdir()
    assert fw._pem_config_arg(repo, tmp, "OPENMV_N6") is None
    assert "could not read the mbedtls config" in capsys.readouterr().err


def test_build_firmware_ota_passes_pem_override_and_leaves_source(make_project, monkeypatch):
    root, repo, _app = make_project(ota=True)
    common = Path(repo) / _COMMON_REL
    port_cfg = Path(repo) / _PORT_REL
    before = (common.read_text(), port_cfg.read_text())
    seen = {}

    def fake(repo_, args):
        if "clean" not in args:
            for a in args:
                if a.startswith("MBEDTLS_CONFIG_FILE="):
                    seen["copy"] = Path(a.split("=", 1)[1].strip('\\"')).read_text()
            target = next(a.split("=", 1)[1] for a in args if a.startswith("TARGET="))
            f = Path(repo_) / "build" / target / "bin" / "firmware.bin"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"FW")
    monkeypatch.setattr(fw, "_run_make", fake)
    fw.build_firmware(root, firmware=repo, boards=["OPENMV_N6"])
    assert "MBEDTLS_PEM_PARSE_C" in seen["copy"]                        # override -> patched copy
    assert (common.read_text(), port_cfg.read_text()) == before        # firmware source untouched


def test_build_firmware_non_ota(make_project, monkeypatch):
    fake = _fake_make(["bin/firmware.bin"])
    monkeypatch.setattr(fw, "_run_make", fake)
    root, repo, _app = make_project()
    results = fw.build_firmware(root, firmware=repo)
    assert len(results) == 1
    r = results[0]
    assert r.board == "OPENMV_N6" and r.ota is False and r.build_dir is None
    assert [o.name for o in r.outputs] == ["OPENMV_N6-firmware.bin"]
    assert r.outputs[0].read_bytes() == b"FW"
    # clean then build (default), build carries the TARGET + -j
    assert fake.calls[0] == ["TARGET=OPENMV_N6", "clean"]
    assert "TARGET=OPENMV_N6" in fake.calls[1] and any(a.startswith("-j") for a in fake.calls[1])
    assert not any(a.startswith("FROZEN_MANIFEST=") for a in fake.calls[1])


def test_build_firmware_collects_bootloader(make_project, monkeypatch):
    # the bootloader binary, when the port builds one, is collected for `flash bootloader`;
    # the AE3 also emits a padded TOC written alongside its bootloader
    fake = _fake_make(["bin/firmware.bin", "bin/bootloader.bin", "bin/firmware_pad.toc"])
    monkeypatch.setattr(fw, "_run_make", fake)
    root, repo, _app = make_project()
    names = [o.name for o in fw.build_firmware(root, firmware=repo)[0].outputs]
    assert "OPENMV_N6-bootloader.bin" in names
    assert "OPENMV_N6-firmware_pad.toc" in names


def test_build_firmware_incremental_skips_clean(make_project, monkeypatch):
    fake = _fake_make(["bin/firmware.bin"])
    monkeypatch.setattr(fw, "_run_make", fake)
    root, repo, _app = make_project()
    fw.build_firmware(root, firmware=repo, incremental=True, jobs=4)
    assert len(fake.calls) == 1                       # no clean
    assert "-j4" in fake.calls[0]


def test_build_firmware_ignores_openmv_bin(make_project, monkeypatch):
    # The bootloader-combined openmv.bin is deliberately not collected; only
    # firmware.bin is. firmware.bin present -> openmv.bin alongside is ignored.
    fake = _fake_make(["bin/firmware.bin", "bin/openmv.bin"])
    monkeypatch.setattr(fw, "_run_make", fake)
    root, repo, _app = make_project()
    r = fw.build_firmware(root, firmware=repo)[0]
    assert [o.name for o in r.outputs] == ["OPENMV_N6-firmware.bin"]


def test_build_firmware_openmv_bin_only_is_no_image(make_project, monkeypatch):
    # openmv.bin without a firmware.bin counts as no firmware image at all.
    monkeypatch.setattr(fw, "_run_make", _fake_make(["bin/openmv.bin"]))
    root, repo, _app = make_project()
    with pytest.raises(BuildError, match="produced no image"):
        fw.build_firmware(root, firmware=repo)


def test_build_firmware_alif_per_core(make_project, monkeypatch):
    fake = _fake_make(["bin/firmware_M55_HP.bin", "bin/firmware_M55_HE.bin", "bin/firmware.toc"])
    monkeypatch.setattr(fw, "_run_make", fake)
    root, repo, _app = make_project(boards=("OPENMV_AE3",))
    r = fw.build_firmware(root, firmware=repo)[0]
    # both cores collected, the bootloader-written .toc ignored
    assert sorted(o.name for o in r.outputs) == ["OPENMV_AE3-firmware-M55_HE.bin", "OPENMV_AE3-firmware-M55_HP.bin"]


def test_build_firmware_ota_injects_boot(make_project, monkeypatch):
    fake = _fake_make(["bin/firmware.bin"])
    monkeypatch.setattr(fw, "_run_make", fake)
    root, repo, _app = make_project(ota=True)
    r = fw.build_firmware(root, firmware=repo, keep_build_dir=True)[0]
    assert r.ota is True and r.build_dir is not None
    # FROZEN_MANIFEST was pointed at our wrapper
    frozen = [a for a in fake.calls[1] if a.startswith("FROZEN_MANIFEST=")]
    assert len(frozen) == 1 and frozen[0].endswith("manifest.py")
    # wrapper includes the board manifest + freezes BOTH boot.py and _ota_config.py
    manifest = (r.build_dir / "manifest.py").read_text()
    assert "include(" in manifest and "boards/OPENMV_N6/manifest.py" in manifest
    assert 'freeze(' in manifest and "boot.py" in manifest and "_ota_config.py" in manifest
    assert "openmv_log.py" in manifest and "openmv_wdt.py" in manifest  # logger + watchdog frozen
    # the real boot.py (not a placeholder) + the generated config + device modules are present
    assert "OtaBoot" in (r.build_dir / "boot.py").read_text()
    cfg = (r.build_dir / "_ota_config.py").read_text()
    assert "TRUSTED_KEYS" in cfg and "PARTITION_SIZE" in cfg and "PRODUCT_ID" in cfg
    assert "ENABLED" in (r.build_dir / "openmv_log.py").read_text()   # the project's copy
    assert "def relax(" in (r.build_dir / "openmv_wdt.py").read_text()


def test_build_firmware_log_falls_back_to_default(make_project, monkeypatch):
    # An OTA project missing its device/openmv_log.py still freezes a logger -- the bundled default.
    monkeypatch.setattr(fw, "_run_make", _fake_make(["bin/firmware.bin"]))
    root, repo, _app = make_project(ota=True)
    (Path(root) / "device" / "openmv_log.py").unlink()
    r = fw.build_firmware(root, firmware=repo, keep_build_dir=True)[0]
    assert "ENABLED" in (r.build_dir / "openmv_log.py").read_text()


def test_ota_config_values(make_project, monkeypatch):
    monkeypatch.setattr(fw, "_run_make", _fake_make(["bin/firmware.bin"]))
    root, repo, _app = make_project(ota=True)
    r = fw.build_firmware(root, firmware=repo, keep_build_dir=True)[0]
    ns = {}
    exec((r.build_dir / "_ota_config.py").read_text(), ns)  # noqa: S102 (generated code)
    assert ns["PARTITION_SIZE"] > 0 and 0 < ns["FRONT_SIZE"] < ns["PARTITION_SIZE"]
    assert ns["OTA_BLOCK"] == 4096
    assert isinstance(ns["PRODUCT_ID"], int) and ns["PRODUCT_ID"] != 0   # OTA pins it
    assert isinstance(ns["PLATFORM_VERSION"], int)
    keys = ns["TRUSTED_KEYS"]
    assert isinstance(keys, dict) and len(keys) == 3   # 2 ota + 1 factory provisioned
    for kid, pub in keys.items():
        assert isinstance(kid, int) and isinstance(pub, bytes) and pub[0] == 0x04


def test_ota_config_excludes_revoked_keys(make_project, monkeypatch):
    from openmv_ota.ota.keys import read_trusted_keys, write_trusted_keys
    from openmv_ota.project.project import ProjectPaths

    monkeypatch.setattr(fw, "_run_make", _fake_make(["bin/firmware.bin"]))
    root, repo, _app = make_project(ota=True)
    tk = ProjectPaths(root).trusted_keys
    keys = read_trusted_keys(tk)
    keys[0].revoked = True
    write_trusted_keys(tk, keys)

    r = fw.build_firmware(root, firmware=repo, keep_build_dir=True)[0]
    ns = {}
    exec((r.build_dir / "_ota_config.py").read_text(), ns)  # noqa: S102
    assert keys[0].key_id not in ns["TRUSTED_KEYS"]
    assert len(ns["TRUSTED_KEYS"]) == len(keys) - 1


def test_build_firmware_ota_cleans_wrapper(make_project, monkeypatch):
    captured = {}
    real_writer = fw._write_wrapper_manifest

    def spy(p, repo, name):
        d = real_writer(p, repo, name)
        captured["dir"] = d
        return d

    monkeypatch.setattr(fw, "_write_wrapper_manifest", spy)
    monkeypatch.setattr(fw, "_run_make", _fake_make(["bin/firmware.bin"]))
    root, repo, _app = make_project(ota=True)
    fw.build_firmware(root, firmware=repo)  # no keep -> wrapper dir removed
    assert not captured["dir"].exists()


def test_install_verify_module_drops_and_is_idempotent(tmp_path):
    repo = tmp_path / "fw"
    repo.mkdir()
    dst = fw._install_verify_module(repo)
    assert dst == repo / "modules" / "ecdsa_verify.c" and dst.exists()
    assert "mbedtls_ecdsa_verify" in dst.read_text()
    assert fw._install_verify_module(repo) is None   # already present -> not clobbered


def test_build_firmware_ota_compiles_then_removes_c_module(make_project, monkeypatch):
    root, repo, _app = make_project(ota=True)
    cmod = repo / "modules" / "ecdsa_verify.c"
    seen = {}

    def make_spy(rp, args):
        if "clean" not in args:                       # the build call
            seen["present"] = cmod.exists()           # module was dropped in before build
            f = repo / "build" / "OPENMV_N6" / "bin" / "firmware.bin"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"FW")

    monkeypatch.setattr(fw, "_run_make", make_spy)
    fw.build_firmware(root, firmware=repo)
    assert seen.get("present") is True                # auto-compiled during the build
    assert not cmod.exists()                          # removed afterwards (tree restored)


def test_build_firmware_non_ota_no_c_module(make_project, monkeypatch):
    monkeypatch.setattr(fw, "_run_make", _fake_make(["bin/firmware.bin"]))
    root, repo, _app = make_project()                 # non-OTA: no module, no config
    fw.build_firmware(root, firmware=repo)
    assert not (repo / "modules" / "ecdsa_verify.c").exists()


def test_build_firmware_no_image_errors(make_project, monkeypatch):
    monkeypatch.setattr(fw, "_run_make", _fake_make([]))  # build produces nothing
    root, repo, _app = make_project()
    with pytest.raises(BuildError, match="produced no image"):
        fw.build_firmware(root, firmware=repo)


def test_build_firmware_no_matching_boards(make_project, monkeypatch):
    monkeypatch.setattr(fw, "_run_make", _fake_make(["bin/firmware.bin"]))
    root, repo, _app = make_project()
    with pytest.raises(BuildError, match="no matching boards"):
        fw.build_firmware(root, firmware=repo, boards=["OPENMV_AE3"])


def test_build_firmware_refuses_on_drift(make_project, git_cmd, monkeypatch):
    monkeypatch.setattr(fw, "_run_make", _fake_make(["bin/firmware.bin"]))
    root, repo, _app = make_project()
    (repo / "newfile.txt").write_text("x")
    git_cmd(repo, "add", "-A")
    git_cmd(repo, "commit", "-q", "-m", "drift")
    with pytest.raises(BuildError, match="refusing to proceed"):
        fw.build_firmware(root, firmware=repo)


def _mpy_cross_dir(repo: Path) -> Path:
    d = repo / "lib" / "micropython" / "mpy-cross"
    d.mkdir(parents=True)
    return d


def test_ensure_mpy_cross_absent_is_noop(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    fw._ensure_mpy_cross(tmp_path)            # no lib/micropython/mpy-cross tree
    assert calls == []


def test_ensure_mpy_cross_already_built_is_noop(tmp_path, monkeypatch):
    built = _mpy_cross_dir(tmp_path) / "build" / "mpy-cross"
    built.parent.mkdir()
    built.write_text("binary")
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    fw._ensure_mpy_cross(tmp_path)
    assert calls == []


def test_ensure_mpy_cross_builds_with_clean_env(tmp_path, monkeypatch):
    d = _mpy_cross_dir(tmp_path)
    monkeypatch.setenv("CFLAGS", "-mcpu=cortex-m7 -mthumb")  # the leak we must drop
    monkeypatch.setenv("CXXFLAGS", "-mthumb")
    seen = {}
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: seen.update(cmd=cmd, kw=kw))
    fw._ensure_mpy_cross(tmp_path)
    assert seen["cmd"] == [fw.MAKE, "-C", str(d)] and seen["kw"]["check"] is True
    env = seen["kw"]["env"]
    assert "CFLAGS" not in env and "CXXFLAGS" not in env   # compiler flags stripped
    assert "PATH" in env                                   # the rest of the env kept


def test_ensure_mpy_cross_make_not_found(tmp_path, monkeypatch):
    _mpy_cross_dir(tmp_path)

    def boom(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(BuildError, match="make not found"):
        fw._ensure_mpy_cross(tmp_path)


def test_ensure_mpy_cross_build_failure(tmp_path, monkeypatch):
    _mpy_cross_dir(tmp_path)

    def boom(*a, **k):
        raise subprocess.CalledProcessError(2, ["make"])

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(BuildError, match="mpy-cross build failed"):
        fw._ensure_mpy_cross(tmp_path)


def test_run_make_success(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: seen.update(cmd=cmd, kw=kw))
    fw._run_make(tmp_path, ["TARGET=X", "clean"])
    assert seen["cmd"] == ["make", "TARGET=X", "clean"] and seen["kw"]["check"] is True


def test_run_make_not_found(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(BuildError, match="make not found"):
        fw._run_make(tmp_path, ["TARGET=X"])


def test_run_make_failed(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise subprocess.CalledProcessError(2, ["make"])

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(BuildError, match="firmware build failed"):
        fw._run_make(tmp_path, ["TARGET=X"])


def test_copy_wifi_blobs_for_arduino(tmp_path):
    # an Arduino board's CYW4343 blobs are copied out of the firmware tree, version-matched
    wdir = tmp_path / "repo" / "drivers" / "cyw4343" / "firmware"
    wdir.mkdir(parents=True)
    (wdir / "cyw4343_7_45_98_102.bin").write_bytes(b"WIFI")
    (wdir / "cyw4343_btfw.bin").write_bytes(b"BT")
    out = tmp_path / "out"
    out.mkdir()
    copied = fw._copy_wifi_blobs(tmp_path / "repo", "ARDUINO_PORTENTA_H7", out)
    assert [p.name for p in copied] == ["cyw4343_7_45_98_102.bin", "cyw4343_btfw.bin"]
    assert (out / "cyw4343_btfw.bin").read_bytes() == b"BT"


def test_copy_wifi_blobs_noop_for_non_arduino(tmp_path):
    assert fw._copy_wifi_blobs(tmp_path, "OPENMV4", tmp_path) == []


def test_copy_wifi_blobs_missing_blob_raises(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(BuildError, match="not found in the firmware tree"):
        fw._copy_wifi_blobs(tmp_path / "repo", "ARDUINO_PORTENTA_H7", out)


# --- the clock floor stamped into _ota_config -------------------------------

def test_build_time_is_the_locks_timestamp_not_the_wall_clock():
    # taken from the lock so a build stays reproducible: same lock, same firmware
    from openmv_ota.build.firmware import _build_time
    lock = type("L", (), {"generated_at": "2026-01-01T00:00:00Z"})()
    assert _build_time(type("P", (), {"lock": lock})()) == 1767225600


def test_build_time_is_zero_when_the_lock_has_no_usable_stamp():
    # no floor is better than a wrong one -- openmv_rtc then reports the clock
    # untrusted rather than trusting a floor it doesn't have
    from openmv_ota.build.firmware import _build_time
    for stamp in ("", "not-a-date", None):
        lock = type("L", (), {"generated_at": stamp})()
        assert _build_time(type("P", (), {"lock": lock})()) == 0

# --- micropython #19348 (ranged romfs erase) carried for v5.0 firmware -----------------



def _mpy_repo(tmp_path, *, name="fw", version="5.0.0", vfs=None):
    """A minimal firmware tree: a version header (so resolve_firmware_version works) and,
    when ``vfs`` is given, ``lib/micropython/extmod/vfs.h`` with that content."""
    repo = tmp_path / name
    (repo / "protocol").mkdir(parents=True)
    maj, minr, pat = version.split(".")
    (repo / "protocol" / "omv_protocol.h").write_text(
        "#define OMV_FIRMWARE_VERSION_MAJOR (%s)\n"
        "#define OMV_FIRMWARE_VERSION_MINOR (%s)\n"
        "#define OMV_FIRMWARE_VERSION_PATCH (%s)\n" % (maj, minr, pat))
    if vfs is not None:
        v = repo / "lib" / "micropython" / "extmod" / "vfs.h"
        v.parent.mkdir(parents=True)
        v.write_text(vfs)
    return repo


def _fake_git(*, present, fail=None, missing=False):
    """Stand in for ``subprocess.run`` on the git calls. ``present`` is the cat-file
    return code (objects already fetched?); ``fail`` names a subcommand to raise
    CalledProcessError on; ``missing`` raises FileNotFoundError (git absent)."""
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if missing:
            raise FileNotFoundError("git")
        sub = next((a for a in argv if a in ("cat-file", "fetch", "cherry-pick")), None)
        if sub == "cat-file":
            return types.SimpleNamespace(returncode=0 if present else 1)
        if fail == sub:
            raise subprocess.CalledProcessError(1, argv)
        return types.SimpleNamespace(returncode=0)

    run.calls = calls
    return run


def test_partial_erase_skips_non_v50(tmp_path, monkeypatch):
    repo = _mpy_repo(tmp_path, version="6.0.0", vfs="")   # v6 -> not our line
    run = _fake_git(present=False)
    monkeypatch.setattr(fw.subprocess, "run", run)
    fw._ensure_partial_erase(repo)
    assert run.calls == []                                # no git touched


def test_partial_erase_skips_without_version_header(tmp_path, monkeypatch):
    repo = tmp_path / "fw"                                # no protocol/omv_protocol.h
    repo.mkdir()
    run = _fake_git(present=False)
    monkeypatch.setattr(fw.subprocess, "run", run)
    fw._ensure_partial_erase(repo)                        # ProjectError -> skip
    assert run.calls == []


def test_partial_erase_skips_when_sentinel_present(tmp_path, monkeypatch):
    repo = _mpy_repo(tmp_path, vfs="#define MP_VFS_ROM_IOCTL_GET_MIN_PREPARE (6)\n")
    run = _fake_git(present=False)
    monkeypatch.setattr(fw.subprocess, "run", run)
    fw._ensure_partial_erase(repo)                        # already carried -> skip
    assert run.calls == []


def test_partial_erase_skips_without_vfs(tmp_path, monkeypatch):
    repo = _mpy_repo(tmp_path)                            # v5.0.0 but no extmod/vfs.h
    run = _fake_git(present=False)
    monkeypatch.setattr(fw.subprocess, "run", run)
    fw._ensure_partial_erase(repo)                        # OSError -> skip
    assert run.calls == []


def test_partial_erase_cherry_picks_when_absent(tmp_path, monkeypatch, capsys):
    repo = _mpy_repo(tmp_path, vfs="#define MP_VFS_ROM_IOCTL_WRITE_COMPLETE (5)\n")
    run = _fake_git(present=False)
    monkeypatch.setattr(fw.subprocess, "run", run)
    fw._ensure_partial_erase(repo)
    subs = [next((a for a in c if a in ("cat-file", "fetch", "cherry-pick")), None)
            for c in run.calls]
    assert subs == ["cat-file", "fetch", "cherry-pick"]  # absent -> fetch then pick
    pick = next(c for c in run.calls if "cherry-pick" in c)
    for sha in fw._PARTIAL_ERASE_COMMITS:
        assert sha in pick                               # all 6 PR commits picked
    assert "user.email=build@openmv.io" in pick          # inline committer identity
    assert "cherry-picking micropython#19348" in capsys.readouterr().out


def test_partial_erase_skips_fetch_when_objects_present(tmp_path, monkeypatch):
    repo = _mpy_repo(tmp_path, vfs="#define MP_VFS_ROM_IOCTL_WRITE_COMPLETE (5)\n")
    run = _fake_git(present=True)                         # objects already local
    monkeypatch.setattr(fw.subprocess, "run", run)
    fw._ensure_partial_erase(repo)
    subs = [next((a for a in c if a in ("cat-file", "fetch", "cherry-pick")), None)
            for c in run.calls]
    assert subs == ["cat-file", "cherry-pick"]           # no fetch


def test_partial_erase_raises_when_git_missing(tmp_path, monkeypatch):
    repo = _mpy_repo(tmp_path, vfs="#define MP_VFS_ROM_IOCTL_WRITE_COMPLETE (5)\n")
    monkeypatch.setattr(fw.subprocess, "run", _fake_git(present=True, missing=True))
    with pytest.raises(BuildError, match="git not found"):
        fw._ensure_partial_erase(repo)


def test_partial_erase_raises_on_cherry_pick_conflict(tmp_path, monkeypatch):
    repo = _mpy_repo(tmp_path, vfs="#define MP_VFS_ROM_IOCTL_WRITE_COMPLETE (5)\n")
    monkeypatch.setattr(fw.subprocess, "run", _fake_git(present=True, fail="cherry-pick"))
    with pytest.raises(BuildError, match="rebased"):
        fw._ensure_partial_erase(repo)

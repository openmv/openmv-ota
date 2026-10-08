"""Tests for ``openmv-ota build firmware`` (the make invocation is mocked -- no
ARM toolchain in CI)."""

from __future__ import annotations

import subprocess
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


def _capture_make(monkeypatch, headers):
    """A fake make that records the build args and, while the wrapper dir still exists,
    the generated mbedtls header the args point at."""
    seen = []

    def fake(repo_, args):
        seen.extend(args)
        for a in args:
            if a.startswith("MBEDTLS_CONFIG_FILE="):
                headers.append(Path(a.split("=", 1)[1].strip('\\"')).read_text())
        if "clean" not in args:
            target = next(a.split("=", 1)[1] for a in args if a.startswith("TARGET="))
            f = Path(repo_) / "build" / target / "bin" / "firmware.bin"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"FW")
    monkeypatch.setattr(fw, "_run_make", fake)
    return seen


def test_an_ota_build_adds_the_mbedtls_speed_options_the_firmware_lacks(make_project, monkeypatch):
    """Without them an AE3 TLS handshake with verification took ~17 s and Cloudflare cut it
    off; an OTA build for a TLS-update board adds them through a wrapper header that
    includes the port's own config unchanged -- the firmware source is never touched."""
    root, repo, _app = make_project(ota=True)
    common = Path(repo) / _COMMON_REL
    before = common.read_text()
    headers = []
    seen = _capture_make(monkeypatch, headers)
    fw.build_firmware(root, firmware=repo, boards=["OPENMV_N6"])

    arg = [a for a in seen if a.startswith("MBEDTLS_CONFIG_FILE=")]
    assert len(arg) == 1 and arg[0].startswith('MBEDTLS_CONFIG_FILE=\\"')   # a C string literal
    (h,) = headers
    assert h.index('#include "mbedtls/mbedtls_config_port.h"') < h.index("#define MBEDTLS_HAVE_ASM")
    for d in fw._MBEDTLS_SPEED:
        assert "#ifndef %s\n#define %s\n#endif" % (d, d) in h
    assert common.read_text() == before             # firmware source untouched, as ever


def test_the_speed_options_fall_away_once_the_firmware_has_them(tmp_path):
    """Upstream took them: once the firmware's own config (common or port) defines every
    option, nothing is injected and the build is exactly the firmware's. One already there
    is just not repeated."""
    from types import SimpleNamespace
    proj = SimpleNamespace(board=lambda name: SimpleNamespace(role="main", mbedtls=True))
    repo = _fake_fw(tmp_path, port="alif")
    out = tmp_path / "wrap"
    out.mkdir()
    port_cfg = repo / "lib" / "micropython" / "ports" / "alif" / "mbedtls" / "mbedtls_config_port.h"
    port_cfg.write_text(port_cfg.read_text() + "#define MBEDTLS_ECP_NIST_OPTIM\n")
    arg = fw._mbedtls_speed_arg(proj, repo, "OPENMV_N6", out)
    h = (out / "mbedtls_config_openmv_ota.h").read_text()
    assert arg and "MBEDTLS_ECP_NIST_OPTIM" not in h and "MBEDTLS_HAVE_ASM" in h

    common = repo / _COMMON_REL
    common.write_text(common.read_text() + "  #define MBEDTLS_HAVE_ASM\n"
                      "#define MBEDTLS_ECP_DP_CURVE25519_ENABLED\n")
    assert fw._mbedtls_speed_arg(proj, repo, "OPENMV_N6", out) is None
    # a mention that is not a define (a comment, an #ifdef) does not count as having it
    common.write_text(common.read_text().replace("  #define MBEDTLS_HAVE_ASM", "// MBEDTLS_HAVE_ASM"))
    assert fw._mbedtls_speed_arg(proj, repo, "OPENMV_N6", out) is not None


def test_the_1792k_boards_get_the_speed_options_too(make_project, monkeypatch):
    """The Nicla, Portenta and Giga cannot carry the CA bundle, but they reach the same
    Cloudflare edge: without the options their handshake stalls past its cutoff just the same.
    Every main-role OTA board whose port builds mbedtls gets them."""
    root, repo, _app = make_project(ota=True, boards=("ARDUINO_NICLA_VISION",))
    monkeypatch.setattr(fw, "_copy_wifi_blobs", lambda *a: [])   # the fake tree has none
    headers = []
    seen = _capture_make(monkeypatch, headers)
    fw.build_firmware(root, firmware=repo)
    assert any(a.startswith("MBEDTLS_CONFIG_FILE=") for a in seen)
    (h,) = headers
    for d in fw._MBEDTLS_SPEED:
        assert "#define %s\n" % d in h


@pytest.mark.parametrize("board", [dict(role="coprocessor", mbedtls=True),
                                   dict(role="main", mbedtls=False)])
def test_no_speed_options_for_a_board_that_does_no_tls(tmp_path, board):
    """A coprocessor core never runs TLS, and a port built without mbedtls has nothing to
    speed up: both are left exactly as they build."""
    from types import SimpleNamespace
    proj = SimpleNamespace(board=lambda name: SimpleNamespace(**board))
    assert fw._mbedtls_speed_arg(proj, tmp_path, "OPENMV_AE3", tmp_path) is None


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
    # RECOVERY must be in the FIRMWARE, not the romfs -- it runs precisely when the romfs is
    # gone. That includes the installer: the romfs copy is the OTA-updatable one, this is the
    # floor a bad update cannot erase.
    for mod in ("openmv_netcfg.py", "openmv_recovery.py", "openmv_installer.py"):
        assert mod in manifest, "%s is not frozen -- recovery could not run" % mod
        assert (r.build_dir / mod).exists()
    # ...and the frozen installer is byte-identical to the one the romfs ships, so a fix
    # cannot land on the normal path and miss the recovery one
    shipped = (Path(fw.__file__).parent / "device" / "openmv_ota" / "data" / "installer.py")
    assert (r.build_dir / "openmv_installer.py").read_bytes() == shipped.read_bytes()
    assert "openmv_log.py" in manifest and "openmv_wdt.py" in manifest  # logger + watchdog frozen
    # No --ca -> no frozen trust store: the public bundle is ~186 KB and freezing it overflows
    # FLASH_TEXT on every 1792 KB board, so it ships in the romfs instead.
    assert "openmv_ca.py" not in manifest
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
    assert ns["CONTROL_BLOCK"] == 4096
    assert ns["MODE"] == "ab"                      # the N6 has room for two slots
    assert ns["MAX_ATTEMPTS"] == 3                 # the trial budget boot.py enforces
    assert isinstance(ns["PRODUCT_ID"], int) and ns["PRODUCT_ID"] != 0   # OTA pins it
    assert isinstance(ns["PLATFORM_VERSION"], int)
    keys = ns["TRUSTED_KEYS"]
    assert isinstance(keys, dict) and len(keys) == 3   # 2 ota + 1 factory provisioned
    for kid, pub in keys.items():
        assert isinstance(kid, int) and isinstance(pub, bytes) and pub[0] == 0x04


def test_ota_config_carries_the_payload_keys_and_the_romfs_does_not(make_project, monkeypatch):
    """The payload key is SECRET, unlike the trusted set beside it. It is baked into
    the firmware and deliberately not into the romfs -- the romfs is the thing being
    downloaded, so a key inside it would be a key anyone who can reach the artifact
    already has."""
    import os

    from openmv_ota.project import payload_keys as pk
    from openmv_ota.project.project import ProjectPaths

    monkeypatch.setattr(fw, "_run_make", _fake_make(["bin/firmware.bin"]))
    root, repo, _app = make_project(ota=True)
    r = fw.build_firmware(root, firmware=repo, keep_build_dir=True)[0]
    ns = {}
    exec((r.build_dir / "_ota_config.py").read_text(), ns)  # noqa: S102 (generated code)

    mine = pk.read(ProjectPaths(root).private_keys_dir, os.environ["OPENMV_OTA_KEY_PASSPHRASE"])
    assert ns["PAYLOAD_KEYS"] == mine["OPENMV_N6"]        # this board's, not another board's
    assert all(len(k) == 32 for k in ns["PAYLOAD_KEYS"].values())


def test_a_board_added_after_the_project_was_created_gets_a_payload_key(make_project, monkeypatch):
    """Adding a board is an ordinary edit, not a key ceremony."""
    import os

    from openmv_ota.project import payload_keys as pk
    from openmv_ota.project.project import ProjectPaths

    monkeypatch.setattr(fw, "_run_make", _fake_make(["bin/firmware.bin"]))
    root, repo, _app = make_project(ota=True)
    private = ProjectPaths(root).private_keys_dir
    phrase = os.environ["OPENMV_OTA_KEY_PASSPHRASE"]
    keys = pk.read(private, phrase)
    del keys["OPENMV_N6"]                                  # as if the board were added later
    keys["OPENMV_AE3"] = {1: b"k" * 32}
    pk.write(private, keys, phrase)

    r = fw.build_firmware(root, firmware=repo, keep_build_dir=True)[0]
    ns = {}
    exec((r.build_dir / "_ota_config.py").read_text(), ns)  # noqa: S102
    assert ns["PAYLOAD_KEYS"] == pk.read(private, phrase)["OPENMV_N6"]
    assert ns["PAYLOAD_KEYS"] != {1: b"k" * 32}            # its own key, not the other board's


def test_a_non_ota_project_needs_no_payload_keys_and_no_passphrase(make_project, monkeypatch):
    """A plain firmware build has nothing to decrypt, so it must not start asking for
    a passphrase to unlock keys that were never minted."""
    monkeypatch.delenv("OPENMV_OTA_KEY_PASSPHRASE", raising=False)
    monkeypatch.setattr(fw, "_run_make", _fake_make(["bin/firmware.bin"]))
    root, repo, _app = make_project(ota=False)
    r = fw.build_firmware(root, firmware=repo, keep_build_dir=True)[0]
    assert r.ota is False and r.build_dir is None


def test_a_build_without_the_payload_keys_says_what_is_missing(make_project, monkeypatch):
    """The file is the fleet's ability to receive updates. A build that cannot find it
    stops and says so, rather than quietly producing firmware that can decrypt nothing."""
    from openmv_ota.project import payload_keys as pk
    from openmv_ota.project.project import ProjectPaths

    monkeypatch.setattr(fw, "_run_make", _fake_make(["bin/firmware.bin"]))
    root, repo, _app = make_project(ota=True)
    pk.path_for(ProjectPaths(root).private_keys_dir).unlink()
    with pytest.raises(BuildError, match="Restore them with"):
        fw.build_firmware(root, firmware=repo)


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

    def spy(p, repo, name, payload_keys):
        d = real_writer(p, repo, name, payload_keys)
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


# --- v2: mode + recovery config stamped into _ota_config ---------------------------------

class _FakeCfg:
    def __init__(self, ca="", server_url="", single_image=False):
        self.ca, self.server_url, self.single_image = ca, server_url, single_image


class _FakeProj:
    def __init__(self, root, **kw):
        self.root, self.config = root, _FakeCfg(**kw)


class _FakeTarget:
    def __init__(self, name="OPENMV_N6", recovery_ca_bundle=False, tls_verify=True):
        self.name, self.recovery_ca_bundle = name, recovery_ca_bundle
        self.tls_verify = tls_verify


def test_recovery_ca_empty_freezes_the_project_bundle_on_a_board_with_room(tmp_path):
    """Most servers sit behind a public CA, so 'unset' has to be a first-class answer -- and it
    must yield REAL anchors: recovery runs when the romfs copy of the bundle is gone, so an
    empty CA_PEM would leave TLS with nothing to verify against, retrying forever. On a board
    flagged recovery_ca_bundle the scaffold's own bundle is frozen in."""
    from openmv_ota.build.firmware import _recovery_ca

    certs = tmp_path / "certs"
    certs.mkdir()
    (certs / "ca.pem").write_bytes(b"-----BEGIN CERTIFICATE-----\nbundle\n")
    big = _FakeTarget(recovery_ca_bundle=True)
    assert _recovery_ca(_FakeProj(tmp_path), big).startswith(b"-----BEGIN")
    assert _recovery_ca(_FakeProj(tmp_path, ca="   "), big).startswith(b"-----BEGIN")


def test_recovery_ca_empty_on_a_board_without_room_fails_the_build_loudly(tmp_path):
    """A 1792 KB board cannot link the ~186 KB bundle (measured: H7 Plus 106.85%, PureThermal
    104.57%, Nicla 101.56%), and shipping recovery with no anchors instead would strand every
    device it recovers -- so the build refuses and says what to set."""
    from openmv_ota.build.errors import BuildError
    from openmv_ota.build.firmware import _recovery_ca

    with pytest.raises(BuildError, match=r"cannot hold the public CA bundle") as e:
        _recovery_ca(_FakeProj(tmp_path), _FakeTarget(name="OPENMV4P"))
    assert "openmv-cloud-roots.pem" in str(e.value)   # says where the hosted roots are


def test_recovery_ca_missing_project_bundle_fails_the_build_loudly(tmp_path):
    """A flagged board with the scaffold bundle deleted must not fall back to empty anchors."""
    from openmv_ota.build.errors import BuildError
    from openmv_ota.build.firmware import _recovery_ca

    with pytest.raises(BuildError, match=r"CA bundle .* is not readable"):
        _recovery_ca(_FakeProj(tmp_path), _FakeTarget(recovery_ca_bundle=True))


def test_recovery_ca_is_read_at_BUILD_time_not_looked_up_on_device(tmp_path):
    """The device must never have to find this file: recovery runs precisely when the
    filesystem that would hold it is gone. So the bytes go into the firmware image."""
    from openmv_ota.build.firmware import _recovery_ca

    (tmp_path / "certs").mkdir()
    (tmp_path / "certs" / "root.pem").write_bytes(b"-----BEGIN CERTIFICATE-----\nxx\n")
    got = _recovery_ca(_FakeProj(tmp_path, ca="certs/root.pem"), _FakeTarget())
    assert got.startswith(b"-----BEGIN")


def test_an_unreadable_ca_fails_the_build_loudly(tmp_path):
    """Silently shipping firmware with no trust anchor would strand every device it recovers:
    it would reach the server and refuse the certificate, with nothing to say why."""
    from openmv_ota.build.errors import BuildError
    from openmv_ota.build.firmware import _recovery_ca

    with pytest.raises(BuildError, match="not readable"):
        _recovery_ca(_FakeProj(tmp_path, ca="certs/missing.pem"), _FakeTarget())


def test_an_unverified_board_without_ca_freezes_no_anchors_and_stamps_false(tmp_path):
    """The M4/M7/H7 (tls_verify false) with [ota].ca unset: an EMPTY CA_PEM -- not the bundle,
    not a refusal -- and TLS_VERIFY False, the one stamp that lets the device connect unverified.
    Every other board stamps True."""
    from openmv_ota.build.firmware import _recovery_ca, _tls_verify_stamp

    legacy = _FakeTarget(name="OPENMV4", tls_verify=False)
    assert _recovery_ca(_FakeProj(tmp_path), legacy) == b""
    assert _tls_verify_stamp(_FakeProj(tmp_path), legacy) is False
    assert _tls_verify_stamp(_FakeProj(tmp_path, ca="  "), legacy) is False
    for t in (_FakeTarget(), _FakeTarget(recovery_ca_bundle=True)):
        assert _tls_verify_stamp(_FakeProj(tmp_path), t) is True


def test_an_unverified_board_with_an_explicit_ca_verifies_and_stamps_true(tmp_path):
    """[ota].ca is an opt-in, and it still works on those boards: its anchors are frozen and
    the stamp is True, so the device verifies (and refuses without anchors) like any other."""
    from openmv_ota.build.firmware import _recovery_ca, _tls_verify_stamp

    (tmp_path / "certs").mkdir()
    (tmp_path / "certs" / "root.pem").write_bytes(b"-----BEGIN CERTIFICATE-----\nxx\n")
    p = _FakeProj(tmp_path, ca="certs/root.pem")
    legacy = _FakeTarget(name="OPENMV4", tls_verify=False)
    assert _recovery_ca(p, legacy).startswith(b"-----BEGIN")
    assert _tls_verify_stamp(p, legacy) is True


@pytest.mark.parametrize(("ca", "want_pem", "want_verify"), [
    (None, b"", False),                   # H7 alone: no anchors, unverified
    ("tiny", b"MIGa", True),              # H7 with --ca: verifies against it
])
def test_ota_config_stamps_tls_verify_for_the_h7(make_project, monkeypatch, ca, want_pem,
                                                 want_verify):
    """End to end through `build firmware`: the generated _ota_config carries the stamp."""
    fake = _fake_make(["bin/firmware.bin"])
    monkeypatch.setattr(fw, "_run_make", fake)
    monkeypatch.setattr(fw, "_ensure_mpy_cross", lambda repo: None)
    root, repo, _app = make_project(boards=("OPENMV4",), ota=True, ca=ca)
    r = fw.build_firmware(root, firmware=repo, boards=["OPENMV4"], keep_build_dir=True)[0]
    ns = {}
    exec((r.build_dir / "_ota_config.py").read_text(), ns)
    assert want_pem in ns["CA_PEM"] and bool(ns["CA_PEM"]) is want_verify
    assert ns["TLS_VERIFY"] is want_verify


def test_build_firmware_freezes_the_configured_trust_store_once(make_project, monkeypatch):
    """[ota].ca is frozen as _ota_config.CA_PEM -- recovery's anchors AND the runtime's
    (builtin_ca) -- and nowhere else. A second frozen copy (the old openmv_ca module) doubled
    the flash cost of a ~17 KB store, and went stale the moment certs/root.pem was replaced;
    a leftover device/openmv_ca.py is ignored."""
    fake = _fake_make(["bin/firmware.bin"])
    monkeypatch.setattr(fw, "_run_make", fake)
    root, repo, _app = make_project(ota=True, boards=("ARDUINO_NICLA_VISION",))
    monkeypatch.setattr(fw, "_copy_wifi_blobs", lambda *a: [])   # the fake tree has none
    (root / "device" / "openmv_ca.py").write_text('PEM = b"stale"\n')
    pem = (root / "certs" / "root.pem").read_bytes()

    r = fw.build_firmware(root, firmware=repo, keep_build_dir=True)[0]

    assert "openmv_ca" not in (r.build_dir / "manifest.py").read_text()
    assert not (r.build_dir / "openmv_ca.py").exists()
    ns = {}
    exec((r.build_dir / "_ota_config.py").read_text(), ns)
    assert ns["CA_PEM"] == pem and b"GTS Root R4" in pem
    assert ns["TLS_VERIFY"] is True                  # a verifying board always stamps True


def test_a_multi_core_board_freezes_the_ota_modules_into_the_main_core_only(make_project,
                                                                            monkeypatch):
    """The AE3's helper core has no mbedtls, never verifies a signature and is never
    updated on its own -- and it was carrying an 18 KB installer it cannot run, which is
    how it came to overflow its FLASH_TEXT by 480 bytes. The port builds both cores from
    one manifest, so the choice has to be made inside it, through the `$(MCU_CORE)` path
    variable the port sets per core."""
    monkeypatch.setattr(fw, "_run_make", _fake_make(["bin/firmware_M55_HP.bin",
                                                     "bin/firmware_M55_HE.bin"]))
    root, repo, _app = make_project(ota=True, boards=["OPENMV_AE3"])
    r = fw.build_firmware(root, firmware=repo, keep_build_dir=True)[0]

    manifest = (r.build_dir / "manifest.py").read_text()
    assert manifest.count("freeze(") == 1                  # one directory, not nine files
    assert '/$(MCU_CORE)")' in manifest
    # the modules are in the main core's directory, and the helper core's is empty
    assert (r.build_dir / "hp" / "boot.py").exists()
    assert (r.build_dir / "hp" / "openmv_installer.py").exists()
    assert (r.build_dir / "hp" / "_ota_config.py").exists()
    assert list((r.build_dir / "he").iterdir()) == []


def test_a_single_core_board_freezes_by_name_as_before(make_project, monkeypatch):
    monkeypatch.setattr(fw, "_run_make", _fake_make(["bin/firmware.bin"]))
    root, repo, _app = make_project(ota=True)              # OPENMV_N6 -> stm32
    r = fw.build_firmware(root, firmware=repo, keep_build_dir=True)[0]
    manifest = (r.build_dir / "manifest.py").read_text()
    assert "$(MCU_CORE)" not in manifest
    assert (r.build_dir / "boot.py").exists() and not (r.build_dir / "hp").exists()


# --- boards whose OTA firmware drops imlib features to fit (the H7): an OVERLAY board dir --

def test_h7_ota_build_overlays_the_board_dir_with_the_drops_off(make_project, monkeypatch):
    """OPENMV4's OTA firmware cannot hold zbar + the datamatrix decoder as well as the frozen
    OTA machinery, so the build hands make a COPY of boards/OPENMV4 with those defines
    commented out (OMV_BOARD_CONFIG_DIR) -- the firmware tree itself is never touched."""
    fake = _fake_make(["bin/firmware.bin"])
    monkeypatch.setattr(fw, "_run_make", fake)
    monkeypatch.setattr(fw, "_ensure_mpy_cross", lambda repo: None)
    root, repo, _app = make_project(boards=("OPENMV4",), ota=True, ca="tiny")
    src_cfg = Path(repo) / "boards" / "OPENMV4" / "imlib_config.h"
    before = src_cfg.read_text()
    seen = {}

    def spy(repo_, args):
        if "clean" in args:
            return fake(repo_, args)
        overlay = next(a.split("=", 1)[1] for a in args if a.startswith("OMV_BOARD_CONFIG_DIR="))
        seen["overlay"] = overlay
        seen["cfg"] = (Path(overlay) / "imlib_config.h").read_text()
        seen["files"] = sorted(p.name for p in Path(overlay).iterdir())
        return fake(repo_, args)
    monkeypatch.setattr(fw, "_run_make", spy)

    fw.build_firmware(root, firmware=repo, boards=["OPENMV4"], keep_build_dir=False)
    assert seen["overlay"].endswith("/board/")             # a trailing slash, like the Makefile's own
    assert "#define IMLIB_ENABLE_QRCODES" in seen["cfg"]   # untouched neighbours
    assert "// #define IMLIB_ENABLE_BARCODES" in seen["cfg"]
    assert "// #define IMLIB_ENABLE_DATAMATRICES" in seen["cfg"]
    assert not any(line.lstrip().startswith("#define IMLIB_ENABLE_BARCODES")
                   for line in seen["cfg"].splitlines())
    assert "board_config.mk" in seen["files"]              # the whole board dir, not one file
    assert src_cfg.read_text() == before                   # firmware source untouched
    assert not Path(seen["overlay"]).exists()              # gone with the wrapper dir


def test_a_board_that_drops_nothing_builds_from_its_own_board_dir(make_project, monkeypatch):
    fake = _fake_make(["bin/firmware.bin"])
    monkeypatch.setattr(fw, "_run_make", fake)
    monkeypatch.setattr(fw, "_ensure_mpy_cross", lambda repo: None)
    root, repo, _app = make_project(ota=True)                   # OPENMV_N6
    fw.build_firmware(root, firmware=repo, boards=["OPENMV_N6"])
    assert not any(a.startswith("OMV_BOARD_CONFIG_DIR=") for a in fake.calls[-1])


def test_board_overlay_refuses_a_firmware_that_disagrees_with_the_table(tmp_path):
    repo = tmp_path / "fw"
    bd = repo / "boards" / "OPENMV4"
    bd.mkdir(parents=True)
    (bd / "board_config.mk").write_text("PORT=stm32\n")
    with pytest.raises(BuildError, match="imlib_config.h not found"):
        fw._board_overlay(repo, "OPENMV4", tmp_path / "t1")
    (bd / "imlib_config.h").write_text("#define IMLIB_ENABLE_BARCODES\n")   # no datamatrices
    with pytest.raises(BuildError, match="does not define IMLIB_ENABLE_DATAMATRICES"):
        fw._board_overlay(repo, "OPENMV4", tmp_path / "t2")
    assert fw._board_overlay(repo, "OPENMV_N6", tmp_path / "t3") is None


def test_speed_options_with_no_port_config_file(tmp_path):
    """A port without its own mbedtls config file is judged on the common config alone."""
    from types import SimpleNamespace
    proj = SimpleNamespace(board=lambda name: SimpleNamespace(role="main", mbedtls=True))
    repo = _fake_fw(tmp_path, port="mimxrt", port_cfg=False)
    assert fw._mbedtls_speed_arg(proj, repo, "OPENMV_N6", tmp_path) is not None


def test_install_key_store_module_is_for_boards_that_keep_their_own_keys(tmp_path):
    repo = tmp_path / "fw"
    repo.mkdir()
    assert fw._install_key_store_module(repo, "OPENMV_N6") is None     # no key area (yet)
    assert fw._install_key_store_module(repo, "OPENMV_RT1060") is None  # a secure element
    dst = fw._install_key_store_module(repo, "OPENMV4")
    assert dst == repo / "modules" / "key_store.c"
    text = dst.read_text()
    assert text.startswith("// Added by `openmv-ota build firmware`: OPENMV4's key area")
    assert "#define OMV_KEY_AREA_ADDR (0x0801FF00UL)\n" in text
    assert fw._KEY_STORE_C.read_text() in text                        # the module, unchanged
    assert fw._install_key_store_module(repo, "OPENMV4") is None       # not clobbered


def test_an_ota_build_of_a_board_with_a_key_area_compiles_the_key_store(make_project,
                                                                        monkeypatch):
    fake = _fake_make(["bin/firmware.bin", "bin/bootloader.bin"])
    monkeypatch.setattr(fw, "_ensure_mpy_cross", lambda repo: None)
    root, repo, _app = make_project(boards=("OPENMV4",), ota=True, ca="tiny")
    kmod = Path(repo) / "modules" / "key_store.c"
    seen = {}

    def spy(repo_, args):
        if "clean" not in args:
            seen["present"] = kmod.exists()
        return fake(repo_, args)
    monkeypatch.setattr(fw, "_run_make", spy)
    fw.build_firmware(root, firmware=repo, boards=["OPENMV4"])
    assert seen["present"] is True and not kmod.exists()             # in for the build, then out


def test_a_bootloader_that_reaches_the_key_area_fails_the_build(make_project, monkeypatch):
    fake = _fake_make(["bin/firmware.bin"])
    monkeypatch.setattr(fw, "_ensure_mpy_cross", lambda repo: None)
    root, repo, _app = make_project(boards=("OPENMV4",), ota=True, ca="tiny")

    def big_boot(repo_, args):
        fake(repo_, args)
        if "clean" not in args:
            boot = Path(repo_) / "build" / "OPENMV4" / "bin" / "bootloader.bin"
            boot.write_bytes(b"\0" * (0x1FF00 + 1))                  # one byte too many
    monkeypatch.setattr(fw, "_run_make", big_boot)
    with pytest.raises(BuildError, match="must stay within 130816.*0x0801FF00"):
        fw.build_firmware(root, firmware=repo, boards=["OPENMV4"])
    assert not (Path(repo) / "modules" / "key_store.c").exists()     # tree restored all the same

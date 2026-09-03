"""Tests for project orchestration: resolve, create, sync, status, setup, load."""

from __future__ import annotations

from pathlib import Path

import pytest

from openmv_ota.project import config as cfg
from openmv_ota.project import lock as lock_mod
from openmv_ota.project import project as proj
from openmv_ota.project.errors import ProjectError

NOW = "2026-01-01T00:00:00Z"


def _create(tmp_path, make_firmware, make_sdk, **over):
    repo = over.pop("repo", None) or make_firmware()
    root = over.pop("root", tmp_path / "proj")
    kwargs = dict(
        firmware=repo, boards=["OPENMV_N6", "OPENMV_AE3"], product=None, vendor=None,
        sdk_home_override=make_sdk(), install_sdk=False, allow_dirty=True, force=False, now=NOW,
        dev=True,   # throwaway dev keys by default (no passphrase to manage); ota-only, else ignored
    )
    kwargs.update(over)
    return root, proj.create_project(root, **kwargs)


# --- resolve_snapshot -------------------------------------------------------

def test_resolve_snapshot_not_git(tmp_path):
    config = cfg.OtaConfig(name="p", vendor=None, boards=["OPENMV_N6"])
    with pytest.raises(ProjectError, match="not a git repository"):
        proj.resolve_snapshot(tmp_path, config, sdk_home_override=None, config_digest="d", now=NOW)


def test_resolve_snapshot_fields(make_firmware, make_sdk):
    config = cfg.OtaConfig(name="p", vendor=None, boards=["OPENMV_N6"])
    lock, warnings = proj.resolve_snapshot(
        make_firmware(), config, sdk_home_override=make_sdk(), config_digest="sha256:d", now=NOW)
    assert lock.firmware["version"] == "5.0.0"
    assert lock.firmware["version_code"] == (5 << 24)
    assert lock.toolchain["vela"]["version"] == "5.0.0"
    n6 = next(r for r in lock.targets["resolved"] if r["name"] == "OPENMV_N6")
    assert n6["geometry_source"] == "firmware"


def test_resolve_snapshot_ae3_dual_partition(make_firmware, make_sdk):
    # A multi-core board resolves *every* partition automatically -- no per-partition
    # config: the coprocessor is slaved to the main core.
    config = cfg.OtaConfig(name="p", vendor=None, boards=["OPENMV_AE3"], overrides={})
    lock, _ = proj.resolve_snapshot(
        make_firmware(), config, sdk_home_override=make_sdk(), config_digest="d", now=NOW)
    resolved = lock.targets["resolved"]
    assert [r["partition_index"] for r in resolved] == [0, 1]
    # Each core has its own geometry, role, and NPU compiler config.
    hp = next(r for r in resolved if r["partition_index"] == 0)
    he = next(r for r in resolved if r["partition_index"] == 1)
    assert hp["role"] == "main" and he["role"] == "coprocessor"
    assert hp["partition_size"] == 25165824 and he["partition_size"] == 1048576
    assert any("ethos-u55-256" in a for a in hp["npu_config"]["args"])
    assert any("ethos-u55-128" in a for a in he["npu_config"]["args"])


def test_resolve_snapshot_partition_size_override_main_only(make_firmware, make_sdk):
    # partition_size overrides only the main partition; the coprocessor keeps its own
    # firmware geometry (there is no per-partition config -- the helper is slaved).
    config = cfg.OtaConfig(name="p", vendor=None, boards=["OPENMV_AE3"],
                           overrides={"OPENMV_AE3": {"partition_size": 12345678}})
    lock, _ = proj.resolve_snapshot(make_firmware(), config, sdk_home_override=make_sdk(),
                                    config_digest="d", now=NOW)
    resolved = {r["partition_index"]: r for r in lock.targets["resolved"]}
    assert resolved[0]["role"] == "main" and resolved[0]["partition_size"] == 12345678
    assert resolved[1]["role"] == "coprocessor" and resolved[1]["partition_size"] == 1048576


def test_resolve_snapshot_unknown_board(make_firmware, make_sdk):
    config = cfg.OtaConfig(name="p", vendor=None, boards=["NOPE"], overrides={})
    with pytest.raises(ProjectError, match="unknown board"):
        proj.resolve_snapshot(make_firmware(), config, sdk_home_override=make_sdk(),
                              config_digest="d", now=NOW)


def test_resolve_snapshot_retired_board(make_firmware, make_sdk):
    # a retired board (pico/ble33) can't be added to a project at all
    config = cfg.OtaConfig(name="p", vendor=None,
                           boards=["ARDUINO_NANO_33_BLE_SENSE"], overrides={})
    with pytest.raises(ProjectError, match="no longer supported"):
        proj.resolve_snapshot(make_firmware(), config, sdk_home_override=make_sdk(),
                              config_digest="d", now=NOW)


# --- ensure_sdk -------------------------------------------------------------

def test_ensure_sdk_ok(make_firmware, make_sdk):
    info = proj.ensure_sdk(make_firmware(), make_sdk(), install_sdk=False)
    assert info.stamp_matches


def test_ensure_sdk_missing_no_install(make_firmware, tmp_path):
    with pytest.raises(ProjectError, match="not installed"):
        proj.ensure_sdk(make_firmware(), tmp_path / "nope", install_sdk=False)


def test_ensure_sdk_mismatch_no_install(make_firmware, make_sdk):
    with pytest.raises(ProjectError, match="but the firmware wants"):
        proj.ensure_sdk(make_firmware(), make_sdk(stamp="9.9.9"), install_sdk=False)


def test_ensure_sdk_install_success(make_firmware, make_sdk, monkeypatch):
    repo = make_firmware()
    home = make_sdk()
    # First resolve sees nothing; the install "creates" it (already created here).
    state = {"made": False}

    def fake_install(version, dest, **kw):
        state["made"] = True

    monkeypatch.setattr(proj.sdk_install, "install_sdk", fake_install)
    # Point at a home that doesn't exist yet, then have install create it by swapping.
    missing = home.parent / "openmv-sdk-missing"
    calls = {"n": 0}
    real_resolve = proj.sdk_res.resolve_sdk

    def fake_resolve(r, override):
        calls["n"] += 1
        # Return the good home on the second call (after install).
        return real_resolve(r, home if calls["n"] >= 2 else missing)

    monkeypatch.setattr(proj.sdk_res, "resolve_sdk", fake_resolve)
    info = proj.ensure_sdk(repo, missing, install_sdk=True)
    assert state["made"] and info.stamp_matches


def test_ensure_sdk_install_still_missing(make_firmware, tmp_path, monkeypatch):
    monkeypatch.setattr(proj.sdk_install, "install_sdk", lambda *a, **k: None)
    with pytest.raises(ProjectError) as ei:
        proj.ensure_sdk(make_firmware(), tmp_path / "nope", install_sdk=True)
    assert ei.value.exit_code == 1


# --- create -----------------------------------------------------------------

def test_create_writes_files(tmp_path, make_firmware, make_sdk):
    root, (lock, warnings) = _create(tmp_path, make_firmware, make_sdk)
    paths = proj.ProjectPaths(root)
    assert paths.config.exists() and paths.lock.exists() and paths.local.exists()
    assert paths.gitignore.exists() and paths.readme.exists()
    # No machine path in the committed files.
    assert "openmv-sdk" not in paths.config.read_text()
    assert "openmv-sdk" not in paths.lock.read_text()
    # AE3 conditional geometry warns.
    assert any("conditional" in w for w in warnings)


def test_create_scaffolds_app_even_without_ota(tmp_path, make_firmware, make_sdk):
    # Every project (OTA or not) gets a starter app/: main.py + settings.json.
    import json
    root, _ = _create(tmp_path, make_firmware, make_sdk, app_version="3.4.5")
    paths = proj.ProjectPaths(root)
    assert (paths.app_dir / "main.py").exists()
    settings = json.loads(paths.app_settings.read_text())
    assert settings["app_version"] == "3.4.5" and "vendor" in settings
    # --vendor must land HERE: settings.json is what the build reads into system.json,
    # so a vendor that only reached the toml would leave the device-visible vendor "".
    # (this create passes no vendor, so the scaffold's default is empty)
    assert settings["vendor"] == ""
    # rollback_floor is a legacy knob no scaffold writes any more -- v2's automatic
    # floor replaced it (the build still reads it from old projects; missing = no floor).
    assert "rollback_floor" not in settings
    # A lib/ dir for the app's own modules, kept in git by a .gitkeep.
    assert (paths.app_dir / "lib").is_dir()
    assert (paths.app_dir / "lib" / ".gitkeep").exists()
    # No keys are provisioned for a non-OTA project.
    assert not paths.private_keys_dir.exists()


def test_vendor_flag_reaches_the_scaffolded_settings(tmp_path, make_firmware, make_sdk):
    """`--vendor` used to land only in openmv-ota.toml, but system.json's vendor is read from
    settings.json -- so the flag's value never reached the device. It seeds settings.json now."""
    import json
    root, _ = _create(tmp_path, make_firmware, make_sdk, vendor="Acme Robotics")
    settings = json.loads(proj.ProjectPaths(root).app_settings.read_text())
    assert settings["vendor"] == "Acme Robotics"


def test_create_scaffolds_coprocessor_for_multicore_board(tmp_path, make_firmware, make_sdk):
    # A board with a slaved second core (AE3's M55_HE) gets an app-coprocessor/ folder;
    # _create targets N6 + AE3, so it must appear.
    import json
    root, _ = _create(tmp_path, make_firmware, make_sdk, app_version="2.0.0")
    d = proj.ProjectPaths(root).coprocessor_app_dir
    assert d.is_dir() and (d / "main.py").exists()
    assert json.loads((d / "settings.json").read_text())["app_version"] == "2.0.0"
    assert (d / "lib" / ".gitkeep").exists()


def test_create_no_coprocessor_folder_for_single_core(tmp_path, make_firmware, make_sdk):
    root, _ = _create(tmp_path, make_firmware, make_sdk, boards=["OPENMV_N6"])
    assert not proj.ProjectPaths(root).coprocessor_app_dir.exists()


def test_create_ota_scaffolds_runtime_lib_with_coprocessor_data(tmp_path, make_firmware, make_sdk):
    # An OTA project gets the device runtime lib; a coprocessor board (AE3, in the
    # default set) also gets the sync() resource manifest + a valid placeholder romfs.
    import json

    from openmv_ota.romfs.builder import read_image
    root, _ = _create(tmp_path, make_firmware, make_sdk, ota=True, ota_keys=2, factory_keys=1)
    lib = proj.ProjectPaths(root).app_dir / "lib" / "openmv_ota"
    assert (lib / "__init__.py").exists()
    # the installer is scaffolded for every OTA board
    assert "def run(" in (lib / "data" / "installer.py").read_text()
    # WITHOUT --ca the PUBLIC bundle scaffolds to certs/ca.pem -- OUTSIDE app/, so it does
    # NOT ship in the romfs. `build firmware` freezes it (this path only exists on
    # recovery_ca_bundle boards) and the runtime reads that one frozen copy via
    # builtin_ca(); shipping it in the romfs too would pay ~186 KB per slot twice.
    assert (root / "certs" / "ca.pem").read_bytes() == proj._fetch_ca_bundle()
    assert not (lib / "data" / "ca.pem").exists()
    assert not (root / "device" / proj.CA_MODULE).exists()
    res = json.loads((lib / "data" / "resources.json").read_text())
    assert res[0]["handler"] == "partition" and res[0]["partition"] == 1
    read_image((lib / "data" / "coprocessor.romfs").read_bytes())   # valid romfs, no raise


def test_create_ota_runtime_lib_no_coprocessor_data_without_coprocessor(
        tmp_path, make_firmware, make_sdk):
    # A plain OTA board still gets data/ for the installer + CA bundle, but no
    # coprocessor resource (nothing to sync).
    root, _ = _create(tmp_path, make_firmware, make_sdk, boards=["OPENMV_N6"],
                      ota=True, ota_keys=2, factory_keys=1)
    lib = proj.ProjectPaths(root).app_dir / "lib" / "openmv_ota"
    data = lib / "data"
    assert (lib / "__init__.py").exists()
    # the openmv_cloud SDK package scaffolds beside openmv_ota (from openmv_cloud import csi).
    assert "class CSI" in (lib.parent / "openmv_cloud" / "csi.py").read_text()
    assert (lib.parent / "openmv_cloud" / "__init__.py").exists()
    assert "def run(" in (data / "installer.py").read_text()
    assert (root / "certs" / "ca.pem").read_bytes() == proj._fetch_ca_bundle()  # the stubbed bundle
    assert not (data / "ca.pem").exists()   # the bundle rides in firmware, not the romfs
    assert not (root / "device" / proj.CA_MODULE).exists()   # no openmv_ca module without --ca
    assert not (data / "coprocessor.romfs").exists()
    assert not (data / "resources.json").exists()


# --- _fetch_ca_bundle (the real downloader; network mocked at urlopen) -------

class _FakeResp:
    def __init__(self, data):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._data


# Captured at import (before the autouse stub swaps the module attribute) so these
# tests exercise the real downloader, with the network mocked at urlopen.
_REAL_FETCH = proj._fetch_ca_bundle


def test_fetch_ca_bundle_success(monkeypatch):
    import urllib.request
    pem = b"-----BEGIN CERTIFICATE-----\nreal\n-----END CERTIFICATE-----\n"
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _FakeResp(pem))
    assert _REAL_FETCH("https://x/ca.pem") == pem


def test_fetch_ca_bundle_network_error(monkeypatch):
    import urllib.error
    import urllib.request

    def boom(*a, **k):
        raise urllib.error.URLError("down")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(ProjectError, match="could not download"):
        _REAL_FETCH("https://x/ca.pem")


def test_fetch_ca_bundle_invalid(monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _FakeResp(b"nope"))
    with pytest.raises(ProjectError, match="looks invalid"):
        _REAL_FETCH("https://x/ca.pem")


def test_create_non_ota_no_runtime_lib(tmp_path, make_firmware, make_sdk):
    root, _ = _create(tmp_path, make_firmware, make_sdk, boards=["OPENMV_N6"])
    assert not (proj.ProjectPaths(root).app_dir / "lib" / "openmv_ota").exists()


def test_create_ota_scaffolds_device_files(tmp_path, make_firmware, make_sdk):
    root, _ = _create(tmp_path, make_firmware, make_sdk, boards=["OPENMV_N6"],
                      ota=True, ota_keys=2, factory_keys=1)
    log = root / "device" / "openmv_log.py"
    wdt = root / "device" / "openmv_wdt.py"
    assert "ENABLED" in log.read_text() and 'getLogger("openmv_ota")' in log.read_text()
    assert "ENABLED" in wdt.read_text() and "def relax(" in wdt.read_text()


def test_create_non_ota_no_device_files(tmp_path, make_firmware, make_sdk):
    root, _ = _create(tmp_path, make_firmware, make_sdk, boards=["OPENMV_N6"])
    assert not (root / "device").exists()
    assert not (root / "compliance").exists()


def test_create_ota_scaffolds_compliance_templates(tmp_path, make_firmware, make_sdk):
    # CRA conformity is per-product, so the fill-in paperwork lands in the project.
    repo = make_firmware()
    root, _ = _create(tmp_path, make_firmware, make_sdk, repo=repo, boards=["OPENMV_N6"],
                      ota=True, ota_keys=2, factory_keys=1)
    d = root / "compliance"
    names = sorted(p.name for p in d.iterdir())
    assert names == ["conformity-assessment-checklist.md", "eu-doc.md",
                     "security.txt", "vuln-disclosure-policy.md"]
    assert "{{PRODUCT_NAME}}" in (d / "eu-doc.md").read_text()
    # A filled-in (renamed-or-edited) file survives new --force; templates are re-seeded
    # only when absent.
    (d / "eu-doc.md").write_text("filled in\n")
    _create(tmp_path, make_firmware, make_sdk, repo=repo, root=root, force=True,
            boards=["OPENMV_N6"], ota=True, ota_keys=2, factory_keys=1)
    assert (d / "eu-doc.md").read_text() == "filled in\n"


def test_create_scaffolds_proprietary_license(tmp_path, make_firmware, make_sdk):
    # The safe default for product firmware: all-rights-reserved, holder = the
    # vendor (falling back to the product name). The customer replaces it with
    # real terms; the SBOM renderer reads whatever is there.
    repo = make_firmware()
    root, _ = _create(tmp_path, make_firmware, make_sdk, repo=repo)
    text = (root / "LICENSE").read_text()
    assert "PROPRIETARY AND CONFIDENTIAL" in text
    assert "All rights reserved." in text
    # a replaced LICENSE survives new --force -- it is the customer's
    (root / "LICENSE").write_text("MIT License\n")
    _create(tmp_path, make_firmware, make_sdk, repo=repo, root=root, force=True)
    assert (root / "LICENSE").read_text() == "MIT License\n"


def test_create_preserves_existing_app(tmp_path, make_firmware, make_sdk):
    # Re-running new --force never clobbers a user's app.
    repo = make_firmware()
    root, _ = _create(tmp_path, make_firmware, make_sdk, repo=repo)
    paths = proj.ProjectPaths(root)
    (paths.app_dir / "main.py").write_text("print('mine')\n")
    paths.app_settings.write_text('{"app_version": "9.9.9"}\n')
    _create(tmp_path, make_firmware, make_sdk, repo=repo, root=root, force=True)
    import json
    assert (paths.app_dir / "main.py").read_text() == "print('mine')\n"
    assert json.loads(paths.app_settings.read_text())["app_version"] == "9.9.9"


def test_create_default_not_ota(tmp_path, make_firmware, make_sdk):
    root, (lock, _) = _create(tmp_path, make_firmware, make_sdk)
    assert lock.ota is False
    assert "# [ota]" in proj.ProjectPaths(root).config.read_text()


def test_create_ota_project(tmp_path, make_firmware, make_sdk):
    root, (lock, _) = _create(tmp_path, make_firmware, make_sdk, ota=True,
                              factory_keys=2, ota_keys=3, app_version="2.1.0")
    assert lock.ota is True
    assert lock.to_dict()["ota"] is True
    paths = proj.ProjectPaths(root)
    assert "[ota]\nenabled = true" in paths.config.read_text()
    assert cfg.load_config(paths.config).signing_key_id == 0x0100  # first ota key is the signer

    # The app version lives in the scaffolded, user-editable settings.json.
    import json
    settings = json.loads(paths.app_settings.read_text())
    assert settings["app_version"] == "2.1.0" and "vendor" in settings
    assert "rollback_floor" not in settings  # legacy knob; the automatic floor replaced it
    assert (paths.app_dir / "main.py").exists()

    # Public set is committed; private PEMs are written for every key, gitignored.
    from openmv_ota.ota import read_trusted_keys
    keys = read_trusted_keys(paths.trusted_keys)
    assert sorted(k.role for k in keys) == ["factory", "factory", "ota", "ota", "ota"]
    pems = sorted(p.name for p in paths.private_keys_dir.glob("*.pem"))
    assert pems == ["factory-0001.pem", "factory-0002.pem",
                    "ota-0100.pem", "ota-0101.pem", "ota-0102.pem"]
    assert "keys/private/" in paths.gitignore.read_text()


def test_editing_product_identity_does_not_drift(tmp_path, make_firmware, make_sdk):
    import re
    repo = make_firmware()
    root, _ = _create(tmp_path, make_firmware, make_sdk, repo=repo, ota=True,
                      factory_keys=1, ota_keys=2)
    paths = proj.ProjectPaths(root)
    # Override the auto-assigned product id (identity, not firmware geometry).
    text = re.sub(r"product_id   = \d+", "product_id   = 12345", paths.config.read_text(), count=1)
    paths.config.write_text(text, encoding="utf-8")
    # No drift: identity lives in config, not the firmware-resolved lock.
    assert proj.status_project(root, firmware=repo) == []


def test_create_ota_no_factory_key_errors(tmp_path, make_firmware, make_sdk):
    with pytest.raises(ProjectError, match="at least one factory key"):
        _create(tmp_path, make_firmware, make_sdk, ota=True, factory_keys=0, ota_keys=2)


def test_ensure_ota_capable_rejects_a_partition_with_no_room_for_control():
    """What is left of the old gate: a partition too small for its control sectors cannot host
    an image in EITHER mode. That is arithmetic, not a policy, so it stays an error."""
    lock = lock_mod.Lock(
        generated_by="t", generated_at="t", config_digest="d", firmware={}, micropython={},
        sdk={}, toolchain={}, submodules=[], ota=True,
        targets={"resolved": [{"name": "TINY", "partition_index": 0, "role": "main",
                               "partition_size": 8192, "erase_size": 4096}]})
    with pytest.raises(ProjectError, match="not OTA-capable"):
        proj._ensure_ota_capable(lock)


def _root_pem(tmp_path):
    """A stand-in for "your own server's root" -- the ~1 KB a single-image board can afford."""
    pem = tmp_path / "root.pem"
    pem.write_bytes(b"-----BEGIN CERTIFICATE-----\nMIIBkTCB+w==\n-----END CERTIFICATE-----\n")
    return str(pem)


def test_create_ota_accepts_a_one_sector_board_in_single_mode(tmp_path, make_firmware, make_sdk):
    """OpenMV4's romfs is a single 128K internal-flash sector, so it cannot hold two slots --
    which used to disqualify it from OTA entirely. Under v2 it builds in single-image mode
    instead: one slot, no fallback, recovery living in the firmware.

    It must be given its own trust store: the public bundle is ~186 KB against a 114,688-byte
    slot, so these boards do OTA against your own server."""
    from openmv_ota.ota import geometry

    root, (lock, _) = _create(tmp_path, make_firmware, make_sdk, ota=True, boards=["OPENMV4"],
                              factory_keys=1, ota_keys=2, ca=_root_pem(tmp_path))
    assert lock.ota is True
    rb = next(t for t in lock.targets["resolved"] if t.get("role", "main") == "main")
    assert geometry.derive_mode(rb["partition_size"], rb["erase_size"]) == geometry.SINGLE
    assert root.exists()
    # the supplied roots are what gets frozen -- NOT the public bundle
    ns = {}
    exec(compile((root / "device" / proj.CA_MODULE).read_text(), "openmv_ca.py", "exec"), ns)
    assert b"BEGIN CERTIFICATE" in ns["PEM"] and len(ns["PEM"]) < 4096
    assert (root / "certs" / "root.pem").exists()    # copied in, so the project is self-contained


def test_create_ota_ca_must_exist(tmp_path, make_firmware, make_sdk):
    with pytest.raises(ProjectError, match="not readable"):
        _create(tmp_path, make_firmware, make_sdk, ota=True, boards=["OPENMV4"],
                factory_keys=1, ota_keys=2, ca=str(tmp_path / "nope.pem"))


def test_create_ota_ca_must_look_like_a_pem(tmp_path, make_firmware, make_sdk):
    """A DER file or a stray binary would fail much later, on the device, as a TLS error."""
    bad = tmp_path / "root.der"
    bad.write_bytes(b"\x30\x82\x01\x0a not pem")
    with pytest.raises(ProjectError, match="does not look like a PEM"):
        _create(tmp_path, make_firmware, make_sdk, ota=True, boards=["OPENMV4"],
                factory_keys=1, ota_keys=2, ca=str(bad))


def test_trust_store_reports_an_unreadable_configured_ca(tmp_path):
    """`[ota] ca` pointing at a path that isn't there -- e.g. hand-edited, or not committed."""
    import types as _t
    paths = proj.ProjectPaths(tmp_path)
    cfg = _t.SimpleNamespace(ca="certs/gone.pem")
    lock = _t.SimpleNamespace(targets={"resolved": []})
    with pytest.raises(ProjectError, match=r"\[ota\] ca .* is not readable"):
        proj._trust_store(paths, cfg, lock)


def test_create_ota_refuses_a_one_sector_board_without_its_own_ca(tmp_path, make_firmware, make_sdk):
    """The public bundle does not fit these boards, so scaffolding it would only move the
    failure to a linker error nobody connects to a certificate. Refuse where it can be
    explained, and say what to pass."""
    with pytest.raises(ProjectError, match="cannot hold the public CA bundle"):
        _create(tmp_path, make_firmware, make_sdk, ota=True, boards=["OPENMV4"],
                factory_keys=1, ota_keys=2)


def test_create_non_ota_allows_non_capable_board(tmp_path, make_firmware, make_sdk):
    # The same board builds fine as a single (non-OTA) image filling the partition.
    root, (lock, _) = _create(tmp_path, make_firmware, make_sdk, boards=["OPENMV4"])
    assert lock.ota is False


def test_create_ota_rejects_no_mbedtls_board(tmp_path, make_firmware, make_sdk):
    # MPS2_AN500 is OTA-capable by geometry but builds without mbedtls
    # (MICROPY_SSL_MBEDTLS = 0), so the device couldn't verify image signatures.
    repo = make_firmware()
    mk = repo / "boards" / "MPS2_AN500" / "board_config.mk"
    mk.parent.mkdir(parents=True, exist_ok=True)
    mk.write_text("CPU=cortex-m7\nMICROPY_SSL_MBEDTLS = 0\n")
    with pytest.raises(ProjectError, match="without mbedtls"):
        _create(tmp_path, make_firmware, make_sdk, repo=repo, ota=True,
                boards=["MPS2_AN500"], factory_keys=1, ota_keys=2)


def test_create_non_ota_allows_no_mbedtls_board(tmp_path, make_firmware, make_sdk):
    # Without --ota the same board is fine (no on-device verify needed).
    repo = make_firmware()
    mk = repo / "boards" / "MPS2_AN500" / "board_config.mk"
    mk.parent.mkdir(parents=True, exist_ok=True)
    mk.write_text("MICROPY_SSL_MBEDTLS = 0\n")
    root, (lock, _) = _create(tmp_path, make_firmware, make_sdk, repo=repo,
                              boards=["MPS2_AN500"])
    assert lock.ota is False


def test_sync_ota_project_rechecks_capability(tmp_path, make_firmware, make_sdk):
    # Re-locking an OTA project re-runs the capability check (capable boards -> ok).
    repo = make_firmware()
    root, _ = _create(tmp_path, make_firmware, make_sdk, repo=repo, ota=True,
                      factory_keys=1, ota_keys=2)
    lock, _ = proj.sync_project(root, firmware=repo, sdk_home_override=make_sdk(),
                                install_sdk=False, allow_dirty=True, now=NOW)
    assert lock.ota is True


def test_create_ota_small_pool_warns(tmp_path, make_firmware, make_sdk):
    _, (_, warnings) = _create(tmp_path, make_firmware, make_sdk, ota=True,
                               factory_keys=1, ota_keys=2)
    assert any("small rotation pool" in w for w in warnings)


def test_ota_requires_key_passphrase(tmp_path, make_firmware, make_sdk):
    with pytest.raises(ProjectError, match="signing keys are encrypted"):
        _create(tmp_path, make_firmware, make_sdk, ota=True, ota_keys=2, factory_keys=1, dev=False)


def test_create_not_git(tmp_path, make_sdk):
    with pytest.raises(ProjectError, match="not a git repository"):
        proj.create_project(
            tmp_path / "p", firmware=tmp_path / "notrepo", boards=["OPENMV_N6"],
            product=None, vendor=None, sdk_home_override=make_sdk(), install_sdk=False,
            allow_dirty=True, force=False, now=NOW)


def test_create_existing_no_force(tmp_path, make_firmware, make_sdk):
    repo = make_firmware()
    root, _ = _create(tmp_path, make_firmware, make_sdk, repo=repo)
    with pytest.raises(ProjectError) as ei:
        _create(tmp_path, make_firmware, make_sdk, repo=repo, root=root)
    assert ei.value.exit_code == 1


def test_create_force_overwrites(tmp_path, make_firmware, make_sdk):
    repo = make_firmware()
    root, _ = _create(tmp_path, make_firmware, make_sdk, repo=repo)
    _, (lock, _) = _create(tmp_path, make_firmware, make_sdk, repo=repo, root=root, force=True)
    assert lock.firmware["version"] == "5.0.0"


def test_create_dirty_warns(tmp_path, make_firmware, make_sdk):
    repo = make_firmware()
    (repo / "SDK_VERSION").write_text("1.6.0\n")  # uncommitted change -> dirty
    root, (lock, warnings) = _create(tmp_path, make_firmware, make_sdk, repo=repo, allow_dirty=False)
    assert lock.firmware["dirty"] is True
    assert any("dirty" in w for w in warnings)


# --- sync / status ----------------------------------------------------------

def test_sync_rewrites_lock(tmp_path, make_firmware, make_sdk):
    repo = make_firmware()
    root, _ = _create(tmp_path, make_firmware, make_sdk, repo=repo)
    lock, warnings = proj.sync_project(
        root, firmware=repo, sdk_home_override=make_sdk(), install_sdk=False, allow_dirty=True, now=NOW)
    assert lock.firmware["version"] == "5.0.0"


def test_sync_dirty_warns(tmp_path, make_firmware, make_sdk):
    repo = make_firmware()
    root, _ = _create(tmp_path, make_firmware, make_sdk, repo=repo)
    (repo / "SDK_VERSION").write_text("1.6.0\n")
    _, warnings = proj.sync_project(
        root, firmware=repo, sdk_home_override=make_sdk(), install_sdk=False, allow_dirty=False, now=NOW)
    assert any("dirty" in w for w in warnings)


def test_status_in_sync_then_drift(tmp_path, make_firmware, make_sdk, git_cmd):
    repo = make_firmware()
    root, _ = _create(tmp_path, make_firmware, make_sdk, repo=repo)
    assert proj.status_project(root, firmware=repo, now=NOW) == []
    # Change the firmware -> a new commit -> drift.
    (repo / "newfile.txt").write_text("x")
    git_cmd(repo, "add", "-A")
    git_cmd(repo, "commit", "-q", "-m", "second")
    changes = proj.status_project(root, firmware=repo, now=NOW)
    assert any("firmware.commit" in c for c in changes)


def test_checkout_path_missing(tmp_path, make_firmware, make_sdk):
    root, _ = _create(tmp_path, make_firmware, make_sdk)
    proj.ProjectPaths(root).local.unlink()  # remove local.toml
    with pytest.raises(ProjectError, match="no firmware checkout"):
        proj.status_project(root, firmware=None, now=NOW)


# --- setup ------------------------------------------------------------------

def test_setup_clones_and_writes_local(tmp_path, make_firmware, make_sdk, monkeypatch):
    repo = make_firmware()
    root, _ = _create(tmp_path, make_firmware, make_sdk, repo=repo)
    proj.ProjectPaths(root).local.unlink()

    clones, subs, installs = [], [], []
    monkeypatch.setattr(proj.gitrepo, "is_git_repo", lambda d: False)
    monkeypatch.setattr(proj.gitrepo, "clone", lambda r, d, commit=None: clones.append((r, d, commit)))
    monkeypatch.setattr(proj.gitrepo, "submodule_update", lambda d: subs.append(d))
    monkeypatch.setattr(proj, "ensure_sdk", lambda *a, **k: None)
    monkeypatch.setattr(proj, "_mpy_cross_installed", lambda: False)
    monkeypatch.setattr(proj.gitrepo, "pip_install", lambda spec: installs.append(spec))

    dest = proj.setup_project(root, cache_override=str(tmp_path / "cache"),
                              sdk_home_override=None, install_sdk=True)
    assert clones and subs
    assert installs == ["mpy-cross==1.28.0"]  # setup provisions mpy-cross too
    assert proj.ProjectPaths(root).local.exists()
    assert dest == clones[0][1]


def test_setup_cache_hit_skips_clone(tmp_path, make_firmware, make_sdk, monkeypatch):
    root, _ = _create(tmp_path, make_firmware, make_sdk)
    clones = []
    monkeypatch.setattr(proj.gitrepo, "is_git_repo", lambda d: True)
    monkeypatch.setattr(proj.gitrepo, "clone", lambda *a, **k: clones.append(a))
    monkeypatch.setattr(proj.gitrepo, "submodule_update", lambda d: None)
    proj.setup_project(root, cache_override=str(tmp_path / "c"), sdk_home_override=None, install_sdk=False)
    assert clones == []


def test_ensure_mpy_cross_skips_when_present(monkeypatch):
    monkeypatch.setattr(proj, "_mpy_cross_installed", lambda: True)
    called = []
    monkeypatch.setattr(proj.gitrepo, "pip_install", lambda s: called.append(s))
    proj._ensure_mpy_cross("1.28.0")
    assert called == []


def test_ensure_mpy_cross_no_version(monkeypatch):
    called = []
    monkeypatch.setattr(proj.gitrepo, "pip_install", lambda s: called.append(s))
    proj._ensure_mpy_cross(None)
    assert called == []


def test_ensure_mpy_cross_failure_warns(monkeypatch, capsys):
    monkeypatch.setattr(proj, "_mpy_cross_installed", lambda: False)

    def boom(spec):
        raise ProjectError("not on PyPI")

    monkeypatch.setattr(proj.gitrepo, "pip_install", boom)
    proj._ensure_mpy_cross("9.9.9")
    assert "warning" in capsys.readouterr().err


def test_mpy_cross_installed_real():
    assert proj._mpy_cross_installed() in (True, False)


def test_setup_lock_no_remote(tmp_path, make_firmware, make_sdk):
    root, _ = _create(tmp_path, make_firmware, make_sdk)
    paths = proj.ProjectPaths(root)
    locked = lock_mod.read(paths.lock)
    locked.firmware["remote"] = None
    lock_mod.write(paths.lock, locked)
    with pytest.raises(ProjectError, match="no firmware remote/commit"):
        proj.setup_project(root, cache_override=str(tmp_path / "c"), sdk_home_override=None, install_sdk=False)


# --- load API ---------------------------------------------------------------

def test_load_project(tmp_path, make_firmware, make_sdk):
    repo = make_firmware()
    home = make_sdk(with_bins=True)
    root, _ = _create(tmp_path, make_firmware, make_sdk, repo=repo, sdk_home_override=home)
    p = proj.load_project(root)
    assert p.firmware_path == repo.resolve()
    assert p.sdk_home == home
    assert p.vela_path.endswith("/bin/vela")
    assert p.stedgeai_path.endswith("/linux/stedgeai")
    assert p.mpy_cross_path is None  # not built
    assert p.board("OPENMV_N6").front_size == (0x01800000 // 2)


def test_load_project_default_sdk_home(tmp_path, make_firmware, make_sdk, monkeypatch):
    repo = make_firmware()
    root, _ = _create(tmp_path, make_firmware, make_sdk, repo=repo)
    # Blank the sdk home in local.toml -> default ~/openmv-sdk-<ver>.
    paths = proj.ProjectPaths(root)
    paths.local.write_text(cfg.render_local(repo, None))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    # verify=False: this test points at a missing default SDK on purpose.
    p = proj.load_project(root, verify=False)
    assert p.sdk_home == tmp_path / "openmv-sdk-1.6.0"


def test_load_project_unknown_board(tmp_path, make_firmware, make_sdk):
    root, _ = _create(tmp_path, make_firmware, make_sdk)
    with pytest.raises(ProjectError, match="not a target"):
        proj.load_project(root).board("OPENMV4")


def test_load_project_firmware_override(tmp_path, make_firmware, make_sdk):
    repo = make_firmware()
    root, _ = _create(tmp_path, make_firmware, make_sdk, repo=repo)
    p = proj.load_project(root, firmware=repo)
    assert p.firmware_path == repo.resolve()


def test_load_project_partition_lookup(tmp_path, make_firmware, make_sdk):
    repo = make_firmware()
    root, _ = _create(tmp_path, make_firmware, make_sdk, repo=repo, boards=["OPENMV_AE3"])
    # Target both AE3 cores via a hand-edited config, then re-lock.
    paths = proj.ProjectPaths(root)
    # The table is scaffolded now -- add the key INSIDE it (a second declaration is a TOML error).
    text = paths.config.read_text()
    assert text.count("[targets.OPENMV_AE3]") == 1
    paths.config.write_text(text.replace(
        "[targets.OPENMV_AE3]\n", "[targets.OPENMV_AE3]\npartitions = [0, 1]\n", 1))
    proj.sync_project(root, firmware=repo, sdk_home_override=make_sdk(),
                      install_sdk=False, allow_dirty=True, now=NOW)
    p = proj.load_project(root, firmware=repo)
    assert {t.partition_index for t in p.targets} == {0, 1}
    assert p.board("OPENMV_AE3", 0).partition_size == 25165824
    assert p.board("OPENMV_AE3", 1).partition_size == 1048576
    with pytest.raises(ProjectError, match="partition 5 is not a target"):
        p.board("OPENMV_AE3", 5)


# --- verification (nothing-changed guarantee) -------------------------------

def test_verify_locked_clean(tmp_path, make_firmware, make_sdk):
    repo = make_firmware()
    root, _ = _create(tmp_path, make_firmware, make_sdk, repo=repo)
    assert proj.verify_locked(root, firmware=repo) == []


def test_verify_locked_drift_on_commit(tmp_path, make_firmware, make_sdk, git_cmd):
    repo = make_firmware()
    root, _ = _create(tmp_path, make_firmware, make_sdk, repo=repo)
    (repo / "x.txt").write_text("x")
    git_cmd(repo, "add", "-A")
    git_cmd(repo, "commit", "-q", "-m", "c2")
    problems = proj.verify_locked(root, firmware=repo)
    assert any("firmware.commit" in p for p in problems)


def test_verify_locked_dirty_even_when_pegged_dirty(tmp_path, make_firmware, make_sdk):
    # Pegged dirty (commit unchanged, dirty true->true => no drift), but verify
    # must still refuse because uncommitted changes aren't captured by the commit.
    repo = make_firmware()
    (repo / "SDK_VERSION").write_text("1.6.0 ")  # uncommitted change before pegging
    root, _ = _create(tmp_path, make_firmware, make_sdk, repo=repo, allow_dirty=True)
    problems = proj.verify_locked(root, firmware=repo)
    assert any("dirty" in p for p in problems)


def test_load_project_verify_refuses_on_drift(tmp_path, make_firmware, make_sdk, git_cmd):
    repo = make_firmware()
    root, _ = _create(tmp_path, make_firmware, make_sdk, repo=repo)
    (repo / "x.txt").write_text("x")
    git_cmd(repo, "add", "-A")
    git_cmd(repo, "commit", "-q", "-m", "c2")
    with pytest.raises(ProjectError, match="refusing to proceed"):
        proj.load_project(root, firmware=repo)


def test_load_project_verify_false_skips(tmp_path, make_firmware, make_sdk, git_cmd):
    repo = make_firmware()
    root, _ = _create(tmp_path, make_firmware, make_sdk, repo=repo)
    (repo / "x.txt").write_text("x")
    git_cmd(repo, "add", "-A")
    git_cmd(repo, "commit", "-q", "-m", "c2")
    p = proj.load_project(root, firmware=repo, verify=False)
    assert p.board("OPENMV_N6").front_size == (0x01800000 // 2)


def test_ota_project_scaffolds_the_cloud_wired_main(tmp_path, make_firmware, make_sdk):
    root, _ = _create(tmp_path, make_firmware, make_sdk, ota=True, ota_keys=2, factory_keys=1)
    main = (proj.ProjectPaths(root).app_dir / "main.py").read_text()
    assert "openmv_ota.run(" in main               # the cloud lifecycle task
    assert "from openmv_cloud import" in main       # the SDK wrappers
    assert "logs.enable()" in main
    assert "datalog.post(" in main                  # a telemetry example
    assert "configure(" in main                     # the tunable RAM limits
    # the app confirms the OTA trial explicitly once it is operational (run() does
    # not auto-confirm), so a bad update rolls back instead of sticking.
    assert "openmv_ota.confirm()" in main
    # the labelled sections tell the user what is scaffolding vs their own code
    assert "GENERATED" in main and "YOUR APP" in main
    # the opt-in watchdog is wired in seamlessly: arm AFTER the slow camera setup (not at
    # import) and feed once per loop iteration -- no-ops until the user turns openmv_wdt on.
    assert "import openmv_wdt" in main
    assert "openmv_wdt.start()" in main
    assert "openmv_wdt.feed()" in main
    # start() comes after cam setup and before the loop; feed() is inside the loop
    assert main.index("openmv_wdt.start()") < main.index("while True:") < main.index("openmv_wdt.feed()")


def test_non_ota_project_scaffolds_the_bare_main(tmp_path, make_firmware, make_sdk):
    root, _ = _create(tmp_path, make_firmware, make_sdk, boards=["OPENMV_N6"])
    main = (proj.ProjectPaths(root).app_dir / "main.py").read_text()
    assert "openmv_ota" not in main               # no OTA lib in a non-OTA project
    assert "time.sleep_ms" in main


# --- OTA-required firmware features: micropython #19348 (ranged romfs erase) -----------

def _fw_repo(tmp_path, *, name="fw", version="5.0.0", vfs=None, wdt=False):
    """A minimal firmware tree: a version header, and (when ``vfs`` is given)
    lib/micropython/extmod/vfs.h with that content plus ports/stm32/machine_wdt.c carrying the H7
    guard block that #19350's fork-compat include fixup patches. ``wdt=True`` marks the watchdog
    features already carried (the stm32 WWDG sentinel symbol + the new alif machine_wdt.c), so a
    tree can be set up with ALL _FW_FEATURES already present."""
    repo = tmp_path / name
    (repo / "protocol").mkdir(parents=True)
    maj, mi, pa = version.split(".")
    (repo / "protocol" / "omv_protocol.h").write_text(
        "#define OMV_FIRMWARE_VERSION_MAJOR (%s)\n"
        "#define OMV_FIRMWARE_VERSION_MINOR (%s)\n"
        "#define OMV_FIRMWARE_VERSION_PATCH (%s)\n" % (maj, mi, pa))
    mpy = repo / "lib" / "micropython"
    if vfs is not None:
        v = mpy / "extmod" / "vfs.h"
        v.parent.mkdir(parents=True)
        v.write_text(vfs)
        stm = mpy / "ports" / "stm32" / "machine_wdt.c"     # #19350's fixup target (LL-bus include anchor)
        stm.parent.mkdir(parents=True)
        body = '#include "py/mphal.h"\n#if defined(STM32H7)\n#define WWDG (WWDG1)\n#endif\n'
        if wdt:
            body += "static machine_wdt_obj_t machine_wwdt = {0};\n"   # sentinel: already carried
        stm.write_text(body)
        # #19084's sentinel is the ALIF half, deliberately: upstream merged the generic
        # py/mpconfig.h define, and sentinelling on that made the prerequisite look carried while
        # ports/alif had nothing -- which silently cost the AE3 machine.WDT. Keep the generic file
        # present-but-unmarked so a regression back to it would fail this suite.
        mpc = mpy / "py" / "mpconfig.h"
        mpc.parent.mkdir(parents=True)
        mpc.write_text("#define MICROPY_PY_MACHINE_MEM_BACKUP (0)\n")   # upstream: always there
        alif = mpy / "ports" / "alif" / "mpconfigport.h"    # #19084 sentinel path (the alif half)
        alif.parent.mkdir(parents=True, exist_ok=True)
        alif.write_text("#define MICROPY_PY_MACHINE_MEM_BACKUP (1)\n" if wdt else "// alif\n")
    if wdt:
        alif = mpy / "ports" / "alif" / "machine_wdt.c"     # #19399 sentinel: this file exists
        alif.parent.mkdir(parents=True, exist_ok=True)
        alif.write_text("// alif machine.WDT\n")
    return repo


_NO_SENTINEL = "#define MP_VFS_ROM_IOCTL_WRITE_COMPLETE (5)\n"


def _fake_run_git(*, present=True, fail=None, fail_sha=None):
    """Stand in for gitrepo.run_git. ``present`` = cat-file result (objects local?);
    ``fail`` names a subcommand that raises ProjectError when run with check=True; ``fail_sha`` fails
    only a cherry-pick that carries that SHA (to conflict ONE feature, e.g. an opt-in one)."""
    calls = []

    def run(repo, *args, check=True):
        calls.append(list(args))
        if "status" in args:
            # A CONFLICTED cherry-pick leaves the tree DIRTY -- that is what tells a real
            # conflict apart from a commit upstream already merged (which leaves it clean).
            # Model it, or every conflict here would read as "already merged" and be skipped.
            return "UU lib/micropython/ports/alif/mpconfigport.h\n"
        sub = next((a for a in args if a in ("cat-file", "fetch", "cherry-pick", "commit")), None)
        if sub == "cat-file":
            return "" if present else None           # run_git returns None on non-zero + check=False
        if sub == "cherry-pick" and fail_sha is not None and fail_sha in args:
            if check:
                raise ProjectError("git cherry-pick failed: conflict")
            return None
        if fail is not None and fail == sub:
            if check:
                raise ProjectError("git %s failed: boom" % sub)
            return None
        return ""

    run.calls = calls
    return run


def _subs(calls):
    return [next((a for a in c if a in ("cat-file", "fetch", "cherry-pick", "commit")), None)
            for c in calls]


def test_ota_fw_features_skips_non_v50(tmp_path, monkeypatch):
    run = _fake_run_git()
    monkeypatch.setattr(proj.gitrepo, "run_git", run)
    proj._ensure_ota_firmware_features(_fw_repo(tmp_path, version="6.0.0", vfs=""), apply=True)
    assert run.calls == []                            # different firmware line -> untouched


def test_ota_fw_features_skips_without_version_header(tmp_path, monkeypatch):
    run = _fake_run_git()
    monkeypatch.setattr(proj.gitrepo, "run_git", run)
    (tmp_path / "fw").mkdir()
    proj._ensure_ota_firmware_features(tmp_path / "fw", apply=True)   # ProjectError -> skip
    assert run.calls == []


def test_ota_fw_features_skips_when_sentinel_present(tmp_path, monkeypatch):
    run = _fake_run_git()
    monkeypatch.setattr(proj.gitrepo, "run_git", run)
    proj._ensure_ota_firmware_features(
        _fw_repo(tmp_path, vfs="#define MP_VFS_ROM_IOCTL_GET_MIN_PREPARE (6)\n", wdt=True), apply=True)
    assert run.calls == []                            # every feature already carried/merged


def test_ota_fw_features_skips_without_vfs(tmp_path, monkeypatch):
    run = _fake_run_git()
    monkeypatch.setattr(proj.gitrepo, "run_git", run)
    proj._ensure_ota_firmware_features(_fw_repo(tmp_path), apply=True)   # OSError -> skip
    assert run.calls == []


def test_ota_fw_features_refuses_when_apply_false(tmp_path, monkeypatch):
    run = _fake_run_git()
    monkeypatch.setattr(proj.gitrepo, "run_git", run)
    with pytest.raises(ProjectError, match="no-firmware-patches"):
        proj._ensure_ota_firmware_features(_fw_repo(tmp_path, vfs=_NO_SENTINEL), apply=False)
    assert run.calls == []                            # a capability check -- mutates nothing


def test_ota_fw_features_skips_optin_when_apply_false(tmp_path, monkeypatch, capsys):
    # Required #19348 present -> no raise; the opt-in watchdog features are absent + --no-firmware-
    # patches -> skipped (not fatal), so opting out still builds, just without the optional capability.
    run = _fake_run_git()
    monkeypatch.setattr(proj.gitrepo, "run_git", run)
    proj._ensure_ota_firmware_features(
        _fw_repo(tmp_path, vfs="#define MP_VFS_ROM_IOCTL_GET_MIN_PREPARE (6)\n"), apply=False)
    assert run.calls == []                            # nothing carried, nothing raised
    out = capsys.readouterr().out
    assert "skipping opt-in firmware feature micropython#19350" in out
    assert "skipping opt-in firmware feature micropython#19084" in out
    assert "skipping opt-in firmware feature micropython#19399" in out


def test_ota_fw_features_cherry_picks_when_absent(tmp_path, monkeypatch, capsys):
    run = _fake_run_git(present=False)
    monkeypatch.setattr(proj.gitrepo, "run_git", run)
    repo = _fw_repo(tmp_path, vfs=_NO_SENTINEL)
    proj._ensure_ota_firmware_features(repo, apply=True)
    # every feature absent -> each: cat-file (miss) -> fetch -> cherry-pick PER COMMIT, in order.
    # Per-commit (rather than one pick carrying all of a feature's SHAs) is what lets a commit
    # upstream has merged be skipped instead of aborting the feature -- see _carry_feature.
    picks = [c for c in run.calls if "cherry-pick" in c and "--abort" not in c and "--skip" not in c]
    assert len(picks) == sum(len(f["commits"]) for f in proj._FW_FEATURES)
    expected = [sha for f in proj._FW_FEATURES for sha in f["commits"]]
    assert [p[-1] for p in picks] == expected          # exactly those SHAs, in order
    assert "user.email=build@openmv.io" in picks[0]
    # #19350's fork-compat fixup added the H7 LL include to machine_wdt.c + made an extra commit
    assert '#include "stm32h7xx_ll_bus.h"' in (
        repo / "lib/micropython/ports/stm32/machine_wdt.c").read_text()
    assert any("fork-compat" in " ".join(c) for c in run.calls)
    out = capsys.readouterr().out
    for feat in proj._FW_FEATURES:
        assert "carrying micropython#%s" % feat["pr"] in out


def test_ota_fw_features_skips_fetch_when_objects_present(tmp_path, monkeypatch):
    run = _fake_run_git(present=True)
    monkeypatch.setattr(proj.gitrepo, "run_git", run)
    proj._ensure_ota_firmware_features(_fw_repo(tmp_path, vfs=_NO_SENTINEL), apply=True)
    assert "fetch" not in _subs(run.calls)             # objects already local -> no fetch
    # ONE cherry-pick per COMMIT, not per feature: a carry applies its pinned SHAs one at a
    # time so that a commit upstream has since merged (an EMPTY cherry-pick) can be skipped
    # instead of aborting the whole feature -- which is how the alif watchdog silently stopped
    # being carried once micropython merged its prerequisite's core commit.
    assert (_subs(run.calls).count("cherry-pick")
            == sum(len(f["commits"]) for f in proj._FW_FEATURES))


def _empty_pick_run(fail_sha, status="", git_dir=True):
    """A run_git where cherry-picking ``fail_sha`` fails and `status --porcelain` reports
    ``status`` -- i.e. the commit is ALREADY in the tree (clean status = an empty cherry-pick)."""
    base = _fake_run_git(fail_sha=fail_sha)

    def run(repo, *args, check=True):
        if "status" in args:
            base.calls.append(list(args))
            return status
        return base(repo, *args, check=check)

    run.calls = base.calls
    return run


def test_ota_fw_features_skips_a_commit_upstream_has_merged(tmp_path, monkeypatch, capsys):
    """A pinned SHA upstream has since MERGED cherry-picks EMPTY. Skip that commit and carry on.

    This is precisely how the alif watchdog stopped being carried without anyone noticing:
    micropython merged the core commit of its prerequisite (#19084), the cherry-pick of that SHA
    went empty, the whole feature aborted, #19399 then had nothing to apply onto -- and because
    opt-in features skip quietly, the AE3 simply lost machine.WDT until its own app crashed on it."""
    first = proj._FW_FEATURES[0]["commits"][0]
    run = _empty_pick_run(first)
    monkeypatch.setattr(proj.gitrepo, "run_git", run)
    repo = _fw_repo(tmp_path, vfs=_NO_SENTINEL)

    proj._ensure_ota_firmware_features(repo, apply=True)

    subs = _subs(run.calls)
    assert "--skip" in [a for c in run.calls for a in c]     # the empty one was skipped...
    assert subs.count("cherry-pick") > len(proj._FW_FEATURES)  # ...and the rest still applied
    assert "is already upstream" in capsys.readouterr().out


@pytest.mark.parametrize("status", [
    " M ports/alif/mpconfigport.h",               # a genuine conflict: the tree is dirty
    None,                                         # cannot tell (git itself failed)
])
def test_ota_fw_features_still_aborts_when_not_merely_empty(tmp_path, monkeypatch, status):
    """Only an EMPTY cherry-pick may be skipped. A real conflict -- or an unreadable status --
    must still abort, or the carry would march past a change that never applied and leave a
    firmware that looks patched and is not."""
    required = proj._FW_FEATURES[0]
    run = _empty_pick_run(required["commits"][0], status=status)
    monkeypatch.setattr(proj.gitrepo, "run_git", run)
    repo = _fw_repo(tmp_path, vfs=_NO_SENTINEL)

    with pytest.raises(ProjectError, match="could not carry"):
        proj._ensure_ota_firmware_features(repo, apply=True)


def test_ota_fw_features_raises_and_aborts_on_conflict(tmp_path, monkeypatch):
    # The REQUIRED feature (#19348, carried first) can't be skipped -- a conflict is fatal.
    run = _fake_run_git(present=True, fail="cherry-pick")
    monkeypatch.setattr(proj.gitrepo, "run_git", run)
    with pytest.raises(ProjectError, match="rebased"):
        proj._ensure_ota_firmware_features(_fw_repo(tmp_path, vfs=_NO_SENTINEL), apply=True)
    assert any("--abort" in c for c in run.calls)     # unwound the partial pick


def test_ota_fw_features_skips_optin_on_conflict(tmp_path, monkeypatch, capsys):
    # An OPT-IN feature whose cherry-pick conflicts on the fork is SKIPPED, not fatal: it needs a
    # prerequisite the firmware predates and carries itself once the base advances (merged upstream).
    # Here #19399 (alif WDT) conflicts; the build is NOT broken -- #19348 + #19350 still carry.
    wdt399 = next(f for f in proj._FW_FEATURES if f["pr"] == "19399")
    run = _fake_run_git(present=True, fail_sha=wdt399["commits"][0])
    monkeypatch.setattr(proj.gitrepo, "run_git", run)
    proj._ensure_ota_firmware_features(_fw_repo(tmp_path, vfs=_NO_SENTINEL), apply=True)   # no raise
    assert any("--abort" in c for c in run.calls)      # the conflicted pick was unwound
    assert "skipping opt-in micropython#19399" in capsys.readouterr().out


def test_apply_fork_fixup_inserts_idempotently_then_raises_on_missing_anchor(tmp_path):
    mpy = tmp_path / "mpy"
    (mpy / "ports" / "stm32").mkdir(parents=True)
    f = mpy / "ports" / "stm32" / "machine_wdt.c"
    f.write_text("#if defined(STM32H7)\n#define WWDG (WWDG1)\n#endif\n")
    args = ("ports/stm32/machine_wdt.c", "#if defined(STM32H7)", '#include "stm32h7xx_ll_bus.h"')
    assert proj._apply_fork_fixup(mpy, *args) is True          # inserts after the anchor
    assert '#if defined(STM32H7)\n#include "stm32h7xx_ll_bus.h"\n' in f.read_text()
    assert proj._apply_fork_fixup(mpy, *args) is False         # already present -> no-op
    f.write_text("no anchor here\n")
    with pytest.raises(ProjectError, match="anchor"):          # anchor gone -> loud, not silent
        proj._apply_fork_fixup(mpy, *args)


def test_feature_present_missing_sentinel_file_reads_as_absent(tmp_path):
    # A string-sentinel feature whose file doesn't exist -> read_text OSError -> not present (carry it).
    feat = {"sentinel_path": "ports/stm32/machine_wdt.c", "sentinel": "machine_wwdt"}
    assert proj._feature_present(tmp_path, feat) is False


def test_create_ota_refuses_firmware_missing_ranged_erase(tmp_path, make_firmware, make_sdk):
    # --no-firmware-patches (firmware_patches=False) turns the auto-apply into a hard check:
    # an OTA project on a firmware lacking the ranged erase is refused, not silently built.
    repo = make_firmware()
    (repo / "lib" / "micropython" / "extmod" / "vfs.h").write_text(_NO_SENTINEL)
    with pytest.raises(ProjectError, match="ranged romfs erase"):
        _create(tmp_path, make_firmware, make_sdk, repo=repo, ota=True, firmware_patches=False)

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
    # A lib/ dir for the app's own modules, kept in git by a .gitkeep.
    assert (paths.app_dir / "lib").is_dir()
    assert (paths.app_dir / "lib" / ".gitkeep").exists()
    # No keys are provisioned for a non-OTA project.
    assert not paths.private_keys_dir.exists()


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
    # the installer + CA bundle are scaffolded for every OTA board
    assert "def run(" in (lib / "data" / "installer.py").read_text()
    assert (lib / "data" / "ca.pem").read_bytes() == proj._fetch_ca_bundle()
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
    assert "def run(" in (data / "installer.py").read_text()
    assert (data / "ca.pem").read_bytes() == proj._fetch_ca_bundle()  # the stubbed bundle
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
    assert settings["rollback_floor"] == "2.1.0"  # starts equal to the version
    assert (paths.app_dir / "main.py").exists()

    # Public set is committed; private PEMs are written for every key, gitignored.
    from openmv_ota.ota import read_trusted_keys
    keys = read_trusted_keys(paths.trusted_keys)
    assert sorted(k.role for k in keys) == ["factory", "factory", "ota", "ota", "ota"]
    pems = sorted(p.name for p in paths.private_keys_dir.glob("*.pem"))
    assert pems == ["factory-0001.pem", "factory-0002.pem",
                    "ota-0100.pem", "ota-0101.pem", "ota-0102.pem"]
    assert "keys/private/" in paths.gitignore.read_text()


def test_editing_board_identity_does_not_drift(tmp_path, make_firmware, make_sdk):
    import re
    repo = make_firmware()
    root, _ = _create(tmp_path, make_firmware, make_sdk, repo=repo, ota=True,
                      factory_keys=1, ota_keys=2)
    paths = proj.ProjectPaths(root)
    # Override the auto-assigned product id (identity, not firmware geometry).
    text = re.sub(r"board_id   = \d+", "board_id   = 12345", paths.config.read_text(), count=1)
    paths.config.write_text(text, encoding="utf-8")
    # No drift: identity lives in config, not the firmware-resolved lock.
    assert proj.status_project(root, firmware=repo) == []


def test_create_ota_no_factory_key_errors(tmp_path, make_firmware, make_sdk):
    with pytest.raises(ProjectError, match="at least one factory key"):
        _create(tmp_path, make_firmware, make_sdk, ota=True, factory_keys=0, ota_keys=2)


def test_create_ota_rejects_non_capable_board(tmp_path, make_firmware, make_sdk):
    # OpenMV4's romfs is a single 128K internal-flash sector -> can't host OTA.
    with pytest.raises(ProjectError, match="not OTA-capable"):
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
    paths.config.write_text(paths.config.read_text() + "\n[targets.OPENMV_AE3]\npartitions = [0, 1]\n")
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

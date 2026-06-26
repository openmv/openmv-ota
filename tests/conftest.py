"""Shared fixtures: a real temp firmware git repo and a fake SDK home tree.

Used by both the project and build test suites. No network, no real toolchains.
"""

from __future__ import annotations

import subprocess

import pytest

OMV_PROTOCOL = (
    "#define OMV_FIRMWARE_VERSION_MAJOR  (5)\n"
    "#define OMV_FIRMWARE_VERSION_MINOR  (0)\n"
    "#define OMV_FIRMWARE_VERSION_PATCH  (0)\n"
)
MPCONFIG = (
    "#define MICROPY_VERSION_MAJOR 1\n"
    "#define MICROPY_VERSION_MINOR 28\n"
    "#define MICROPY_VERSION_MICRO 0\n"
    "#define MICROPY_VERSION_PRERELEASE 0\n"
)
PERSISTENT = "#define MPY_VERSION 6\n#define MPY_SUB_VERSION 3\n"
N6_BOARD = (
    '#define OMV_BOARD_TYPE "N6"\n'
    "#define OMV_ROMFS_PART0_ORIGIN 0x70800000\n"
    "#define OMV_ROMFS_PART0_LENGTH 0x01800000\n"
)
AE3_BOARD = (
    '#define OMV_BOARD_TYPE "AE3"\n'
    "#if CORE_M55_HP\n"
    "#define OMV_ROMFS_PART0_LENGTH 0x01800000\n"
    "#define OMV_ROMFS_PART1_LENGTH 0x00100000\n"
    "#elif CORE_M55_HE\n"
    "#define OMV_ROMFS_PART0_LENGTH 0x00100000\n"
    "#endif\n"
)


# A minimal but structurally-valid CA bundle, stubbed in for the network download that
# `project new --ota` does (the real fetcher is covered directly in test_project).
FAKE_CA_BUNDLE = b"-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n"


@pytest.fixture(autouse=True)
def _stub_ca_bundle(monkeypatch):
    """Creating an OTA project downloads the CA root bundle; stub that network call
    everywhere so tests stay offline. The dedicated fetch tests import the real
    function directly, so this attribute swap doesn't affect them."""
    from openmv_ota.project import project as _proj
    monkeypatch.setattr(_proj, "_fetch_ca_bundle", lambda *a, **k: FAKE_CA_BUNDLE)


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def git_cmd():
    """The bare ``git`` helper, for tests that need to mutate a fixture repo."""
    return git


@pytest.fixture
def make_firmware(tmp_path):
    """Create a real temp git repo mimicking an openmv checkout.

    ``omit`` drops a file to exercise error branches; ``with_remote`` toggles the
    origin; ``with_mpy_cross`` commits a stub mpy-cross binary so the project
    resolves a path for it.
    """
    def _make(name="fw", *, sdk_version="1.6.0", with_remote=True, with_mpy_cross=False, omit=()):
        repo = tmp_path / name
        (repo / "protocol").mkdir(parents=True)
        if "omv_protocol" not in omit:
            (repo / "protocol" / "omv_protocol.h").write_text(OMV_PROTOCOL)
        if "sdk_version" not in omit:
            (repo / "SDK_VERSION").write_text(sdk_version)
        mp = repo / "lib" / "micropython" / "py"
        mp.mkdir(parents=True)
        if "mpconfig" not in omit:
            (mp / "mpconfig.h").write_text(MPCONFIG)
        (mp / "persistentcode.h").write_text(PERSISTENT)
        for board, content in (("OPENMV_N6", N6_BOARD), ("OPENMV_AE3", AE3_BOARD)):
            d = repo / "boards" / board
            d.mkdir(parents=True)
            (d / "board_config.h").write_text(content)
        if with_mpy_cross:
            mc = repo / "lib" / "micropython" / "mpy-cross" / "build"
            mc.mkdir(parents=True)
            (mc / "mpy-cross").write_text("")
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "t@t")
        git(repo, "config", "user.name", "t")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "init")
        if with_remote:
            git(repo, "remote", "add", "origin", "git@github.com:openmv/openmv.git")
        return repo
    return _make


@pytest.fixture
def make_sdk(tmp_path):
    """Create a fake SDK home tree."""
    def _make(version="1.6.0", *, vela="5.0.0", stedgeai="0400",
              with_bins=False, windows_layout=False, stamp=...):
        home = tmp_path / ("openmv-sdk-%s" % version)
        home.mkdir(parents=True, exist_ok=True)
        (home / "sdk.version").write_text(version if stamp is ... else stamp)
        if vela:
            sp = home / "python" / ("Lib/site-packages" if windows_layout
                                    else "lib/python3.11/site-packages")
            (sp / ("ethos_u_vela-%s.dist-info" % vela)).mkdir(parents=True, exist_ok=True)
            if with_bins:
                bindir = home / "python" / ("Scripts" if windows_layout else "bin")
                bindir.mkdir(parents=True, exist_ok=True)
                (bindir / ("vela.exe" if windows_layout else "vela")).write_text("")
        if stedgeai:
            sd = home / "stedgeai" / ("stedgeai%s" % stedgeai)
            sd.mkdir(parents=True, exist_ok=True)
            if with_bins:
                u = sd / "Utilities" / ("windows" if windows_layout else "linux")
                u.mkdir(parents=True)
                (u / ("stedgeai.exe" if windows_layout else "stedgeai")).write_text("")
        return home
    return _make

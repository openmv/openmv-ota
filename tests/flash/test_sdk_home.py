"""`flash` finds the project's SDK the way a build does (regression from the AE3 bring-up)."""

from __future__ import annotations

from openmv_ota.flash import flash as fl


def test_cli_finds_the_projects_sdk_without_sdk_home(tmp_path, monkeypatch):
    """Regression (AE3 node): a project made with --install-sdk leaves [sdk].home empty, the
    SDK lives at ~/openmv-sdk-<SDK_VERSION>, and `flash` only ever looked at --sdk-home --
    "dfu-util not found" with the tool in ~/openmv-sdk-1.7.3/bin."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    fw = tmp_path / "openmv"
    fw.mkdir()
    (fw / "SDK_VERSION").write_text("1.7.3\n")
    sdk_bin = tmp_path / "home" / "openmv-sdk-1.7.3" / "bin"
    sdk_bin.mkdir(parents=True)
    (sdk_bin / "dfu-util").write_text("#!/bin/sh\n")
    root = tmp_path / "proj"
    root.mkdir()
    (root / "openmv-ota.local.toml").write_text(
        '[firmware]\npath = "%s"\n\n[sdk]\nhome = ""\n' % fw.as_posix())
    monkeypatch.setattr("shutil.which", lambda name: None)         # nothing on PATH
    import argparse

    from openmv_ota.flash import cli as fcli
    args = argparse.Namespace(sdk_home=None, project=str(root))
    assert fcli._sdk_home(args) == tmp_path / "home" / "openmv-sdk-1.7.3"
    assert fl.tools.find_dfu_util(None, fcli._sdk_home(args)) == str(sdk_bin / "dfu-util")


def test_cli_sdk_home_flag_wins_and_list_has_no_project(tmp_path):
    import argparse

    from openmv_ota.flash import cli as fcli
    assert fcli._sdk_home(argparse.Namespace(sdk_home="/opt/sdk", project=".")) == \
        __import__("pathlib").Path("/opt/sdk")
    assert fcli._sdk_home(argparse.Namespace(sdk_home=None)) is None          # `flash list`
    assert fcli._sdk_home(argparse.Namespace(sdk_home=None, project=str(tmp_path))) is None

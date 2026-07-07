"""The signing-key passphrase resolver (dev file / --key-passphrase-file / env / none)."""

from __future__ import annotations

import pytest

from openmv_ota.project.errors import ProjectError
from openmv_ota.project.passphrase import ENV_VAR, dev_passphrase_path, resolve_passphrase


def _dev(root, value="devpw"):
    dev_passphrase_path(root).parent.mkdir(parents=True, exist_ok=True)
    dev_passphrase_path(root).write_text(value + "\n")


def test_dev_file_wins(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_VAR, "envpw")
    _dev(tmp_path)
    assert resolve_passphrase(tmp_path) == ("devpw", "dev")   # dev file beats env


def test_passphrase_file(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    pf = tmp_path / "pf"
    pf.write_text("filepw\n")
    assert resolve_passphrase(tmp_path, passphrase_file=pf) == ("filepw", "user")


def test_env(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_VAR, "envpw")
    assert resolve_passphrase(tmp_path) == ("envpw", "user")


def test_none_raises(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    with pytest.raises(ProjectError, match="encrypted"):
        resolve_passphrase(tmp_path, interactive=False)

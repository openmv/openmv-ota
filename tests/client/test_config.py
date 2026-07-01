"""The saved client profile: path resolution, round-trip, flag>env>file precedence."""

from __future__ import annotations

import pytest

from openmv_ota.client import config
from openmv_ota.client.errors import ClientError


def test_config_path_honors_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config.config_path() == tmp_path / "openmv-ota" / "client.toml"


def test_config_path_defaults_to_home(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config.config_path() == tmp_path / ".config" / "openmv-ota" / "client.toml"


def test_save_load_remove_roundtrip(tmp_path):
    p = tmp_path / "client.toml"
    assert config.save("https://ota.example/", "tok123", path=p) == p
    assert (p.stat().st_mode & 0o777) == 0o600           # secrets file is 0600
    cfg = config.load(p)
    assert cfg.server_url == "https://ota.example/" and cfg.token == "tok123"
    assert config.remove(p) is True and not p.exists()
    assert config.remove(p) is False                     # idempotent


def test_load_missing_or_malformed(tmp_path):
    assert config.load(tmp_path / "nope.toml") is None
    bad = tmp_path / "bad.toml"
    bad.write_text("not = valid = toml")
    assert config.load(bad) is None


def test_resolve_flag_over_env_over_file(tmp_path, monkeypatch):
    p = tmp_path / "c.toml"
    config.save("https://file", "filetok", path=p)
    monkeypatch.delenv("OPENMV_OTA_SERVER", raising=False)
    monkeypatch.delenv("OPENMV_OTA_TOKEN", raising=False)
    r = config.resolve(None, None, path=p)               # file
    assert r.server_url == "https://file" and r.token == "filetok"
    monkeypatch.setenv("OPENMV_OTA_SERVER", "https://env")
    monkeypatch.setenv("OPENMV_OTA_TOKEN", "envtok")
    r = config.resolve(None, None, path=p)               # env > file
    assert r.server_url == "https://env" and r.token == "envtok"
    r = config.resolve("https://flag/", "flagtok", path=p)   # flag > env, and the / is stripped
    assert r.server_url == "https://flag" and r.token == "flagtok"


def test_resolve_missing_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENMV_OTA_SERVER", raising=False)
    monkeypatch.delenv("OPENMV_OTA_TOKEN", raising=False)
    none = tmp_path / "none.toml"
    with pytest.raises(ClientError, match="server URL"):
        config.resolve(None, None, path=none)
    with pytest.raises(ClientError, match="API token"):
        config.resolve("https://s", None, path=none)

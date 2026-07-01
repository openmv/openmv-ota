"""`openmv-ota client` CLI: login (flag/env/stdin) + logout."""

from __future__ import annotations

import io

from openmv_ota.cli import main
from openmv_ota.client import config


def test_client_no_subcommand_returns_help(capsys):
    assert main(["client"]) == 1


def test_login_with_flag_token(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert main(["client", "login", "--server", "https://ota/", "--token", "abc"]) == 0
    cfg = config.load()
    assert cfg.server_url == "https://ota" and cfg.token == "abc"    # trailing / stripped
    assert "saved" in capsys.readouterr().out


def test_login_token_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OPENMV_OTA_TOKEN", "envtok")
    assert main(["client", "login", "--server", "https://ota"]) == 0
    assert config.load().token == "envtok"


def test_login_token_from_stdin(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("OPENMV_OTA_TOKEN", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO("stdintok\n"))
    assert main(["client", "login", "--server", "https://ota"]) == 0
    assert config.load().token == "stdintok"


def test_login_no_token_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("OPENMV_OTA_TOKEN", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert main(["client", "login", "--server", "https://ota"]) == 2
    assert "no token" in capsys.readouterr().err


def test_logout(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config.save("https://ota", "t")
    assert main(["client", "logout"]) == 0
    assert "removed" in capsys.readouterr().out
    assert main(["client", "logout"]) == 0
    assert "no saved profile" in capsys.readouterr().out

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


def test_scope_is_validated_locally_against_the_same_set_the_api_uses():
    """A typo'd scope should fail at the prompt, not after a round trip.

    The API validates `scopes` against ALL_SCOPES and 400s on an unknown one. `server token
    issue` has always caught that locally via `choices`; `client token issue` did not, so the
    identical mistake cost a request and came back as a server error instead of a usage message.
    """
    import pytest

    from openmv_ota.cli import build_parser
    from openmv_ota.server.scopes import ALL_SCOPES

    p = build_parser()
    with pytest.raises(SystemExit):                      # near-miss for "observe"
        p.parse_args(["client", "token", "issue", "--account-id", "a", "--name", "n",
                      "--scope", "observer"])
    ns = p.parse_args(["client", "token", "issue", "--account-id", "a", "--name", "n",
                       "--scope", "observe"])
    assert ns.scope == ["observe"] and set(ALL_SCOPES) >= set(ns.scope)


def test_client_cli_imports_without_the_server_extra():
    """`client` must work on a base install. It now imports `server.scopes` for `--scope`
    choices, which is safe ONLY because that module (and `server/__init__`) pull in nothing --
    a stray FastAPI import there would break every client command on a plain `pip install`."""
    import builtins
    import importlib

    blocked = ("fastapi", "starlette", "pydantic", "uvicorn")
    real = builtins.__import__

    def guard(name, *a, **k):
        if name.split(".")[0] in blocked:
            raise ImportError("simulated base install: no %s" % name)
        return real(name, *a, **k)

    builtins.__import__ = guard
    try:
        for m in ("openmv_ota.server.scopes", "openmv_ota.client.cli"):
            importlib.reload(importlib.import_module(m))
    finally:
        builtins.__import__ = real


def test_login_server_from_env(tmp_path, monkeypatch):
    """login honours OPENMV_OTA_SERVER like every other verb -- a CI setup that exports the
    pair should not need flags at all."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OPENMV_OTA_SERVER", "https://ota.cloud.openmv.io/")
    monkeypatch.setenv("OPENMV_OTA_TOKEN", "envtok")
    assert main(["client", "login"]) == 0
    cfg = config.load()
    assert cfg.server_url == "https://ota.cloud.openmv.io" and cfg.token == "envtok"


def test_login_defaults_to_the_hosted_service(tmp_path, monkeypatch):
    """A fresh pip install has no flags, env, or profile -- login (and every verb through
    resolve()) falls back to the OpenMV-hosted service, so `login --token T` just works."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("OPENMV_OTA_SERVER", raising=False)
    assert main(["client", "login", "--token", "abc"]) == 0
    assert config.load().server_url == "https://ota.cloud.openmv.io"


def test_resolve_defaults_to_the_hosted_service(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("OPENMV_OTA_SERVER", raising=False)
    cfg = config.resolve(None, "tok", path=tmp_path / "absent.toml")
    assert cfg.server_url == config.DEFAULT_SERVER_URL and cfg.token == "tok"

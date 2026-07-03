"""`openmv-ota server` CLI: the check preflight + graceful degradation."""

from __future__ import annotations

from openmv_ota.cli import main
from openmv_ota.server import _extras
from openmv_ota.server import cli as server_cli
from openmv_ota.server.errors import ServerError
from openmv_ota.server.scopes import SCOPES


def _clear(monkeypatch):
    for k in ("OPENMV_OTA_SWD_IDS_VERIFY_URL", "OPENMV_OTA_SWD_IDS_VERIFY_TOKEN",
              "OPENMV_OTA_STORAGE_BACKEND", "OPENMV_OTA_STORAGE_LOCATION"):
        monkeypatch.delenv(k, raising=False)


def test_server_no_subcommand_returns_help(capsys):
    assert main(["server"]) == 1


def test_check_ok(monkeypatch, capsys):
    _clear(monkeypatch)
    monkeypatch.setenv("OPENMV_OTA_SWD_IDS_VERIFY_URL", "https://swd/api/ota/verify")
    monkeypatch.setenv("OPENMV_OTA_SWD_IDS_VERIFY_TOKEN", "S3CR3T-value")
    assert main(["server", "check"]) == 0
    out = capsys.readouterr().out
    assert "ok" in out and "swd_ids_verify_token = ***" in out and "S3CR3T-value" not in out


def test_check_missing_settings(monkeypatch, capsys):
    _clear(monkeypatch)
    assert main(["server", "check"]) == 2
    assert "missing required settings" in capsys.readouterr().err


def test_check_requires_server_extra(monkeypatch, capsys):
    monkeypatch.setattr(_extras, "require_server_extra",
                        lambda *a, **k: (_ for _ in ()).throw(ServerError("need extra", 2)))
    assert main(["server", "check"]) == 2
    assert "need extra" in capsys.readouterr().err


def _db(tmp_path):
    return "sqlite:///" + str(tmp_path / "ota.db")


def _store(tmp_path):
    from openmv_ota.server.metastore import SqliteMetadataStore
    return SqliteMetadataStore(str(tmp_path / "ota.db"))


def test_migrate_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPENMV_OTA_DATABASE_URL", _db(tmp_path))
    assert main(["server", "migrate"]) == 0
    assert "migrated to schema v" in capsys.readouterr().out


def test_init_cli_seeds_and_keeps_salt(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPENMV_OTA_DATABASE_URL", _db(tmp_path))
    monkeypatch.delenv("OPENMV_OTA_COHORT_SALT", raising=False)
    assert main(["server", "init"]) == 0
    assert "initialized (schema v" in capsys.readouterr().out
    s = _store(tmp_path)
    salt = s.get_meta("cohort_salt")
    s.close()
    assert salt and len(salt) == 32                  # secrets.token_hex(16)
    assert main(["server", "init"]) == 0             # idempotent: salt not regenerated
    s2 = _store(tmp_path)
    assert s2.get_meta("cohort_salt") == salt
    s2.close()


def test_init_uses_configured_salt(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENMV_OTA_DATABASE_URL", _db(tmp_path))
    monkeypatch.setenv("OPENMV_OTA_COHORT_SALT", "fixed-salt")
    assert main(["server", "init"]) == 0
    s = _store(tmp_path)
    assert s.get_meta("cohort_salt") == "fixed-salt"
    s.close()


def test_migrate_requires_extra(monkeypatch, capsys):
    monkeypatch.setattr(_extras, "require_server_extra",
                        lambda *a, **k: (_ for _ in ()).throw(ServerError("need extra", 2)))
    assert main(["server", "migrate"]) == 2
    assert "need extra" in capsys.readouterr().err


def test_init_requires_extra(monkeypatch, capsys):
    monkeypatch.setattr(_extras, "require_server_extra",
                        lambda *a, **k: (_ for _ in ()).throw(ServerError("need extra", 2)))
    assert main(["server", "init"]) == 2
    assert "need extra" in capsys.readouterr().err


def test_run_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENMV_OTA_DATABASE_URL", _db(tmp_path))
    monkeypatch.setenv("OPENMV_OTA_STORAGE_LOCATION", str(tmp_path / "blobs"))
    served: dict = {}
    monkeypatch.setattr(server_cli, "_serve",
                        lambda app, host, port, proxies: served.update(
                            app=app, host=host, port=port, proxies=proxies))
    assert main(["server", "run", "--host", "127.0.0.1", "--port", "1234"]) == 0
    assert served["host"] == "127.0.0.1" and served["port"] == 1234 and served["app"] is not None
    assert served["proxies"] == "127.0.0.1"                 # default trusted-proxy setting threads through


def test_run_requires_extra(monkeypatch, capsys):
    monkeypatch.setattr(_extras, "require_server_extra",
                        lambda *a, **k: (_ for _ in ()).throw(ServerError("need extra", 2)))
    assert main(["server", "run"]) == 2
    assert "need extra" in capsys.readouterr().err


def test_token_issue_list_revoke(tmp_path, monkeypatch, capsys):
    from openmv_ota.server.auth import hash_token
    monkeypatch.setenv("OPENMV_OTA_DATABASE_URL", _db(tmp_path))
    assert main(["server", "token", "issue", "--name", "ci", "--scope", "release:write"]) == 0
    out = capsys.readouterr()
    token = out.out.strip()
    assert token and "store it now" in out.err
    s = _store(tmp_path)
    t = s.get_token(hash_token(token))
    assert t["name"] == "ci" and t["scopes"] == ["release:write"]
    thash = t["token_hash"]
    s.close()
    assert main(["server", "token", "list"]) == 0
    listed = capsys.readouterr().out
    assert thash[:16] in listed and "ci" in listed and "release:write" in listed
    assert main(["server", "token", "revoke", thash]) == 0
    assert "revoked" in capsys.readouterr().out
    s2 = _store(tmp_path)
    assert s2.get_token(thash)["revoked"] == 1
    s2.close()


def test_token_issue_default_scopes(tmp_path, monkeypatch, capsys):
    from openmv_ota.server.auth import hash_token
    monkeypatch.setenv("OPENMV_OTA_DATABASE_URL", _db(tmp_path))
    assert main(["server", "token", "issue", "--name", "full"]) == 0
    token = capsys.readouterr().out.strip()
    s = _store(tmp_path)
    assert s.get_token(hash_token(token))["scopes"] == list(SCOPES)
    s.close()


def test_token_issue_with_account(tmp_path, monkeypatch, capsys):
    from openmv_ota.server.auth import hash_token
    monkeypatch.setenv("OPENMV_OTA_DATABASE_URL", _db(tmp_path))
    assert main(["server", "token", "issue", "--name", "scoped", "--account", "acct_x"]) == 0
    token = capsys.readouterr().out.strip()
    s = _store(tmp_path)
    assert s.get_token(hash_token(token))["account_id"] == "acct_x"
    s.close()


def test_account_create_and_list(tmp_path, monkeypatch, capsys):
    from openmv_ota.server.auth import hash_token
    monkeypatch.setenv("OPENMV_OTA_DATABASE_URL", _db(tmp_path))
    assert main(["server", "account", "create", "--name", "DroneCo"]) == 0
    out = capsys.readouterr()
    assert "account created" in out.err and "store it now" in out.err
    account_id, token = out.out.strip().split()
    assert account_id.startswith("acct_")
    s = _store(tmp_path)
    assert s.get_account(account_id)["name"] == "DroneCo"
    tok = s.get_token(hash_token(token))
    assert tok["account_id"] == account_id and tok["scopes"] == list(SCOPES)
    s.close()
    assert main(["server", "account", "list"]) == 0
    listed = capsys.readouterr().out
    assert account_id in listed and "DroneCo" in listed


def test_account_no_subcommand(capsys):
    assert main(["server", "account"]) == 1


def test_account_requires_extra(monkeypatch, capsys):
    monkeypatch.setattr(_extras, "require_server_extra",
                        lambda *a, **k: (_ for _ in ()).throw(ServerError("need extra", 2)))
    assert main(["server", "account", "create", "--name", "x"]) == 2
    assert main(["server", "account", "list"]) == 2
    assert "need extra" in capsys.readouterr().err


def test_token_no_subcommand(capsys):
    assert main(["server", "token"]) == 1


def test_token_requires_extra(monkeypatch, capsys):
    monkeypatch.setattr(_extras, "require_server_extra",
                        lambda *a, **k: (_ for _ in ()).throw(ServerError("need extra", 2)))
    assert main(["server", "token", "issue", "--name", "x"]) == 2
    assert main(["server", "token", "revoke", "abc"]) == 2
    assert main(["server", "token", "list"]) == 2
    assert "need extra" in capsys.readouterr().err


def test_init_seeds_generated_bootstrap_token(tmp_path, monkeypatch, capsys):
    from openmv_ota.server.auth import hash_token
    monkeypatch.setenv("OPENMV_OTA_DATABASE_URL", _db(tmp_path))
    monkeypatch.delenv("OPENMV_OTA_ADMIN_BOOTSTRAP_TOKEN", raising=False)
    assert main(["server", "init"]) == 0
    err = capsys.readouterr().err
    assert "admin bootstrap token" in err
    token = err.split("admin bootstrap token (store it now): ")[1].strip()
    s = _store(tmp_path)
    assert s.count_tokens() == 1 and s.get_token(hash_token(token))["name"] == "bootstrap"
    s.close()
    assert main(["server", "init"]) == 0                 # idempotent -> no second token
    s2 = _store(tmp_path)
    assert s2.count_tokens() == 1
    s2.close()


def test_init_uses_env_bootstrap_token(tmp_path, monkeypatch, capsys):
    from openmv_ota.server.auth import hash_token
    monkeypatch.setenv("OPENMV_OTA_DATABASE_URL", _db(tmp_path))
    monkeypatch.setenv("OPENMV_OTA_ADMIN_BOOTSTRAP_TOKEN", "my-root-token")
    assert main(["server", "init"]) == 0
    assert "admin bootstrap token" not in capsys.readouterr().err   # provided -> not printed
    s = _store(tmp_path)
    assert s.get_token(hash_token("my-root-token"))["name"] == "bootstrap"
    s.close()

"""`openmv-ota server` CLI: the check preflight + graceful degradation."""

from __future__ import annotations

from openmv_ota.cli import main
from openmv_ota.server import _extras
from openmv_ota.server.errors import ServerError


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

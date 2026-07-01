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

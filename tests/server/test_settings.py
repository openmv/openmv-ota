"""ServerSettings: env + programmatic overrides, required-field validation, secret redaction."""

from __future__ import annotations

from openmv_ota.server.settings import ServerSettings


def test_missing_reports_required_registration_fields():
    s = ServerSettings(swd_ids_verify_url="", swd_ids_verify_token="", storage_backend="local")
    assert set(s.missing()) == {"swd_ids_verify_url", "swd_ids_verify_token"}


def test_missing_empty_when_configured():
    assert ServerSettings(swd_ids_verify_url="https://r", swd_ids_verify_token="t").missing() == []


def test_unverified_boards_default_covers_arduino_and_m4_not_m7():
    s = ServerSettings(swd_ids_verify_url="u", swd_ids_verify_token="t")
    assert {"OPENMV2", "ARDUINO_PORTENTA_H7", "ARDUINO_GIGA", "ARDUINO_NICLA_VISION",
            "ARDUINO_NANO_33_BLE_SENSE", "ARDUINO_NANO_RP2040_CONNECT"} <= s.unverified_boards
    assert "OPENMV3" not in s.unverified_boards          # M7 is registered -- must verify


def test_s3_backend_requires_bucket():
    s = ServerSettings(storage_backend="s3", s3_bucket="",
                       swd_ids_verify_url="u", swd_ids_verify_token="t")
    assert "s3_bucket" in s.missing()


def test_unknown_backend_flagged():
    s = ServerSettings(storage_backend="weird", swd_ids_verify_url="u", swd_ids_verify_token="t")
    assert any("storage_backend" in m for m in s.missing())


def test_summary_redacts_secrets():
    text = "\n".join(ServerSettings(swd_ids_verify_token="supersecret",
                                    swd_ids_verify_url="u").summary())
    assert "swd_ids_verify_token = ***" in text and "supersecret" not in text
    assert "swd_ids_verify_url = u" in text


def test_summary_hides_test_downgrade_hook_when_off_shouts_when_on():
    off = "\n".join(ServerSettings(swd_ids_verify_url="u").summary())
    assert "test_offer_downgrades" not in off               # not a normal knob -> hidden while off
    on = "\n".join(ServerSettings(swd_ids_verify_url="u", test_offer_downgrades=True).summary())
    assert "test_offer_downgrades = True  <-- TEST MODE, never in production" in on


def test_render_port_and_database_url_aliases(monkeypatch):
    monkeypatch.delenv("OPENMV_OTA_PORT", raising=False)
    monkeypatch.delenv("OPENMV_OTA_DATABASE_URL", raising=False)
    monkeypatch.setenv("PORT", "9999")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    s = ServerSettings()
    assert s.port == 9999 and s.database_url == "postgresql://x"


def test_kwargs_override_env(monkeypatch):
    monkeypatch.setenv("PORT", "1000")
    assert ServerSettings(port=8080).port == 8080          # programmatic override wins over env

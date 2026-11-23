"""ServerSettings: env + programmatic overrides, required-field validation, secret redaction."""

from __future__ import annotations

from openmv_ota.server.settings import ServerSettings


def test_registration_is_optional_not_missing():
    """A self-host that cannot reach OpenMV's registration server still deploys -- it
    degrades to read-only serving instead of failing preflight."""
    s = ServerSettings(swd_ids_verify_url="", swd_ids_verify_token="", storage_backend="local")
    assert s.missing() == []


def test_missing_empty_when_configured():
    assert ServerSettings(swd_ids_verify_url="https://r", swd_ids_verify_token="t").missing() == []


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
    text = "\n".join(ServerSettings(keykeeper_token="kk-secret", datalake_admin_token="dl-secret",
                                    swd_ids_verify_url="u").summary())
    assert "kk-secret" not in text and "dl-secret" not in text


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


def test_summary_keeps_the_database_password_to_itself():
    out = "\n".join(ServerSettings(database_url="postgresql://ota:hunter2@db.internal:5432/ota",
                                   swd_ids_verify_url="u").summary())
    assert "hunter2" not in out and "postgresql://ota:***@db.internal:5432/ota" in out


def test_summary_keeps_every_url_s_credentials_to_itself():
    """A credential rides in a URL before the host or in its query, and in any setting that
    is one. A password may hold an @ of its own."""
    out = "\n".join(ServerSettings(
        database_url="postgresql://ota:p@ss@db.internal/ota?sslmode=require&password=hunter2",
        s3_endpoint_url="https://tok3n@s3.internal/", swd_ids_verify_url="https://r.example/v?token=abc"
    ).summary())
    assert "database_url = postgresql://ota:***@db.internal/ota?***" in out
    assert "s3_endpoint_url = https://***@s3.internal/" in out
    assert "swd_ids_verify_url = https://r.example/v?***" in out
    for secret in ("ss@", "hunter2", "tok3n", "abc"):
        assert secret not in out
    for url in ("postgresql://u:pa#ss@h:5432/db", "postgresql://u:pa?ss@h/db",
                "postgresql://u:pa/ss@h/db#frag"):
        line = ServerSettings(database_url=url, swd_ids_verify_url="u").summary()
        text = [x for x in line if x.startswith("database_url")][0]
        assert "ss" not in text and "frag" not in text and "u:***@h" in text

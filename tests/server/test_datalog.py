"""Datalake ingest grants minted at check-in.

The token subject binds the account (``account/device``) inside the MAC, so a
device can't attribute data to another account -- re-verified here exactly as
the datalake's tokens.py does.
"""

import hashlib
import hmac

import pytest
from fastapi.testclient import TestClient

from openmv_ota.server import datalog
from openmv_ota.server.app import create_app
from openmv_ota.server.metastore import SqliteMetadataStore
from openmv_ota.server.settings import ServerSettings
from openmv_ota.server.storage import LocalArtifactStorage
from openmv_ota.server.verify import Registration

DATALAKE = "https://data.cloud.openmv.io"
SECRET = "shared-secret"


class _Verifier:
    def __init__(self, registered=True):
        self._reg = Registration(registered, "o1")

    def verify(self, board, device_id):
        return self._reg


def _app(tmp_path, *, registered=True, **overrides):
    store = SqliteMetadataStore(str(tmp_path / "ota.db"))
    store.migrate()
    store.set_meta("cohort_salt", "test-secret")
    storage = LocalArtifactStorage(str(tmp_path / "blobs"))
    settings = ServerSettings(base_url="https://ota.test", checkin_rate_per_min=0,
                              swd_ids_verify_url="u", swd_ids_verify_token="t", **overrides)
    return create_app(settings, storage=storage, metastore=store,
                      verifier=_Verifier(registered))


CHECKIN = {"device_id": "cam-42", "product_id": 7, "board": "OPENMV_N6"}


def relay_verify(token, role, subject):
    exp, _, mac = token.partition(".")
    want = hmac.new(SECRET.encode(), f"{role}:{subject}:{exp}".encode(),
                    hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, mac)


# --- the grant ---------------------------------------------------------------

@pytest.mark.parametrize("overrides", [
    {},
    {"datalake_url": DATALAKE},                       # secret missing
    {"live_token_secret": SECRET},                   # url missing
])
def test_ingest_grant_none_unless_configured(overrides):
    s = ServerSettings(swd_ids_verify_url="u", swd_ids_verify_token="t", **overrides)
    assert datalog.ingest_grant(s, "acct1", "cam-42") is None


def test_ingest_grant_binds_account_in_the_token():
    s = ServerSettings(swd_ids_verify_url="u", swd_ids_verify_token="t",
                       datalake_url=DATALAKE + "/", live_token_secret=SECRET,
                       live_token_ttl=1234)
    g = datalog.ingest_grant(s, "acct1", "cam-42")
    assert g["expires_in_s"] == 1234
    assert g["url"] == "https://data.cloud.openmv.io/api/v1/ingest/acct1/cam-42"
    assert relay_verify(g["token"], "ingest", "acct1/cam-42")
    assert not relay_verify(g["token"], "ingest", "other/cam-42")   # account bound


def test_ingest_grant_empty_account_falls_back_to_default():
    s = ServerSettings(swd_ids_verify_url="u", swd_ids_verify_token="t",
                       datalake_url=DATALAKE, live_token_secret=SECRET)
    g = datalog.ingest_grant(s, "", "cam-42")
    assert g["url"].endswith("/api/v1/ingest/default/cam-42")
    assert relay_verify(g["token"], "ingest", "default/cam-42")


# --- the check-in integration ------------------------------------------------

def test_checkin_carries_ingest_grant_when_configured(tmp_path):
    app = _app(tmp_path, datalake_url=DATALAKE, live_token_secret=SECRET)
    r = TestClient(app).post("/api/v1/check", json=CHECKIN)
    assert r.status_code == 200
    g = r.json()["ingest"]
    assert g["url"].endswith("/cam-42")
    assert relay_verify(g["token"], "ingest", "default/cam-42")  # no account bound yet -> default


def test_checkin_without_datalake_config_omits_ingest(tmp_path):
    r = TestClient(_app(tmp_path)).post("/api/v1/check", json=CHECKIN)
    assert "ingest" not in r.json()


def test_unregistered_device_gets_no_ingest_grant(tmp_path):
    app = _app(tmp_path, registered=False, datalake_url=DATALAKE, live_token_secret=SECRET)
    r = TestClient(app).post("/api/v1/check", json=CHECKIN)
    assert "ingest" not in r.json()


def test_unverified_board_bypass_gets_no_ingest_grant(tmp_path):
    app = _app(tmp_path, datalake_url=DATALAKE, live_token_secret=SECRET,
               unverified_boards={"OPENMV2"})
    r = TestClient(app).post("/api/v1/check",
                             json={"device_id": "legacy-1", "product_id": 7, "board": "OPENMV2"})
    assert "ingest" not in r.json()


def test_settings_accept_fleet_wide_datalake_url(monkeypatch):
    monkeypatch.setenv("OPENMV_DATALAKE_URL", DATALAKE)
    s = ServerSettings(swd_ids_verify_url="u", swd_ids_verify_token="t")
    assert s.datalake_url == DATALAKE

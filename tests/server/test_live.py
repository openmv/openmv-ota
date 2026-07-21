"""OpenMV Live camera grants: token minting + the check-in integration.

The token algorithm must stay in lockstep with the relay's verifier
(openmv-cloud services/live-relay src/auth.ts): exp.hex(hmac_sha256(secret,
"role:device_id:exp")) -- re-verified here from first principles.
"""

import hashlib
import hmac

import pytest
from fastapi.testclient import TestClient

from openmv_ota.server import live
from openmv_ota.server.app import create_app
from openmv_ota.server.metastore import SqliteMetadataStore
from openmv_ota.server.settings import ServerSettings
from openmv_ota.server.storage import LocalArtifactStorage
from openmv_ota.server.verify import Registration

RELAY = "https://live.cloud.openmv.io"
SECRET = "live-test-secret"


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
                              swd_ids_verify_url="u", swd_ids_verify_token="t",
                              **overrides)
    return create_app(settings, storage=storage, metastore=store,
                      verifier=_Verifier(registered))


CHECKIN = {"device_id": "cam-42", "product_id": 7, "board": "OPENMV_N6"}


def relay_verify(token: str, role: str, device_id: str) -> bool:
    exp, _, mac = token.partition(".")
    want = hmac.new(SECRET.encode(), f"{role}:{device_id}:{exp}".encode(),
                    hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, mac)


# --- minting ---------------------------------------------------------------------------------

def test_mint_token_verifies_like_the_relay():
    tok = live.mint_token(SECRET, "camera", "cam-42", 600, now=1_900_000_000)
    exp, _, _ = tok.partition(".")
    assert exp == str(1_900_000_000 + 600)
    assert relay_verify(tok, "camera", "cam-42")
    assert not relay_verify(tok, "viewer", "cam-42")
    assert not relay_verify(tok, "camera", "cam-43")


@pytest.mark.parametrize("overrides", [
    {},                                              # neither configured
    {"live_relay_url": RELAY},                       # secret missing
    {"live_token_secret": SECRET},                   # relay URL missing
])
def test_camera_grant_none_unless_fully_configured(overrides):
    settings = ServerSettings(swd_ids_verify_url="u", swd_ids_verify_token="t", **overrides)
    assert live.camera_grant(settings, "cam-42") is None


def test_camera_grant_builds_ready_made_urls_per_stream():
    settings = ServerSettings(swd_ids_verify_url="u", swd_ids_verify_token="t",
                              live_relay_url=RELAY + "/", live_token_secret=SECRET,
                              live_token_ttl=1234)
    g = live.camera_grant(settings, "cam-42", ["0", "tele"])
    assert g["expires_in_s"] == 1234
    assert set(g["streams"]) == {"0", "tele"}
    s0, tele = g["streams"]["0"], g["streams"]["tele"]
    assert s0["camera_url"].startswith("wss://live.cloud.openmv.io/camera/cam-42/0?token=")
    assert s0["poll_url"].startswith("https://live.cloud.openmv.io/poll/cam-42/0?token=")
    assert tele["camera_url"].startswith("wss://live.cloud.openmv.io/camera/cam-42/tele?token=")
    token = s0["camera_url"].rsplit("token=", 1)[1]
    assert relay_verify(token, "camera", "cam-42")
    # ONE device credential: every stream URL carries the same token.
    assert tele["camera_url"].rsplit("token=", 1)[1] == token
    assert s0["poll_url"].rsplit("token=", 1)[1] == token


def test_camera_grant_defaults_to_the_single_stream():
    settings = ServerSettings(swd_ids_verify_url="u", swd_ids_verify_token="t",
                              live_relay_url=RELAY, live_token_secret=SECRET)
    assert set(live.camera_grant(settings, "d1")["streams"]) == {"0"}
    assert set(live.camera_grant(settings, "d1", [])["streams"]) == {"0"}


def test_camera_grant_sanitizes_stream_names():
    settings = ServerSettings(swd_ids_verify_url="u", swd_ids_verify_token="t",
                              live_relay_url=RELAY, live_token_secret=SECRET)
    # path-hostile / non-string / duplicate names are dropped, the cap applies,
    # and an all-invalid report falls back to the default stream.
    g = live.camera_grant(settings, "d1", ["ok", "../evil", "a/b", "", 7, "ok", "x" * 65])
    assert set(g["streams"]) == {"ok"}
    assert set(live.camera_grant(settings, "d1", ["../e"])["streams"]) == {"0"}
    over = live.camera_grant(settings, "d1", ["s%d" % i for i in range(20)])
    assert len(over["streams"]) == live._MAX_STREAMS


def test_camera_grant_http_relay_becomes_ws():  # local/dev relays (wrangler dev)
    settings = ServerSettings(swd_ids_verify_url="u", swd_ids_verify_token="t",
                              live_relay_url="http://localhost:8787", live_token_secret=SECRET)
    g = live.camera_grant(settings, "d1")
    assert g["streams"]["0"]["camera_url"].startswith("ws://localhost:8787/camera/d1/0?token=")
    assert g["streams"]["0"]["poll_url"].startswith("http://localhost:8787/poll/d1/0?token=")


# --- the check-in integration ----------------------------------------------------------------

def test_checkin_carries_live_grant_when_configured(tmp_path):
    app = _app(tmp_path, live_relay_url=RELAY, live_token_secret=SECRET)
    r = TestClient(app).post("/api/v1/check", json=CHECKIN)
    assert r.status_code == 200
    g = r.json()["live"]
    url = g["streams"]["0"]["camera_url"]
    assert relay_verify(url.rsplit("token=", 1)[1], "camera", "cam-42")


def test_checkin_reported_streams_shape_the_grant(tmp_path):
    app = _app(tmp_path, live_relay_url=RELAY, live_token_secret=SECRET)
    r = TestClient(app).post("/api/v1/check", json=dict(CHECKIN, streams=["front", "tele"]))
    assert set(r.json()["live"]["streams"]) == {"front", "tele"}


def test_checkin_without_live_config_omits_the_key(tmp_path):
    r = TestClient(_app(tmp_path)).post("/api/v1/check", json=CHECKIN)
    assert r.status_code == 200
    assert "live" not in r.json()


def test_unregistered_device_gets_no_live_grant(tmp_path):
    app = _app(tmp_path, registered=False, live_relay_url=RELAY, live_token_secret=SECRET)
    r = TestClient(app).post("/api/v1/check", json=CHECKIN)
    assert r.status_code == 200
    assert "live" not in r.json()


def test_unverified_board_bypass_gets_no_live_grant(tmp_path):
    app = _app(tmp_path, live_relay_url=RELAY, live_token_secret=SECRET,
               unverified_boards={"OPENMV2"})
    r = TestClient(app).post("/api/v1/check",
                             json={"device_id": "legacy-1", "product_id": 7, "board": "OPENMV2"})
    assert r.status_code == 200
    assert "live" not in r.json()


def test_settings_accept_the_fleet_wide_env_names(monkeypatch):
    monkeypatch.setenv("OPENMV_LIVE_RELAY_URL", RELAY)
    monkeypatch.setenv("OPENMV_LIVE_TOKEN_SECRET", SECRET)
    s = ServerSettings(swd_ids_verify_url="u", swd_ids_verify_token="t")
    assert s.live_relay_url == RELAY
    assert s.live_token_secret == SECRET

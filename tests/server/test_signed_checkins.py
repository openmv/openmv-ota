"""Signed check-ins on the server.

A registered camera signs its check-in with the identity key its registration names. Only a
signed check-in gets the full treatment; an unsigned one -- or one signed at a stale time -- gets
update offers and nothing else, and a bad signature gets nothing. The good ones are noted on the
camera's registration, the bad ones counted there and audited.
"""

from __future__ import annotations

import json
import time

from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from openmv_ota.server.app import create_app
from openmv_ota.server.auth import hash_token
from openmv_ota.server.metastore import SqliteMetadataStore
from openmv_ota.server.settings import ServerSettings
from openmv_ota.server.storage import LocalArtifactStorage
from openmv_ota.server.verify import Registration
from tests.server import camera

BID = 7
ACCT = "acct_example"                 # a placeholder, not a real account
DEV = "OPENMV_N6:dev1"
NOTHING = {"update": False, "poll_after_s": 3600}


class _Verifier:
    def verify(self, board, device_id):
        return Registration(True)


def _app(tmp_path):
    store = SqliteMetadataStore(str(tmp_path / "ota.db"))
    store.migrate()
    store.set_meta("capability_secret", "x")
    store.add_account(ACCT, "Example")
    store.add_token(hash_token("tok"), "owner", ["observe"], account_id=ACCT)
    store.add_release(release_id="rel1", product_id=BID, product="P", version="2.0.0",
                      payload_version=0x02000000, min_platform_version=0, image_sha256="ab" * 32,
                      image_size=3, representations=[{"format": "full", "url": "x.img.gz",
                                                      "size": 3}],
                      manifest_key="m/rel1", image_key="i/rel1", account_id=ACCT)
    store.add_rollout(rollout_id="ro1", release_id="rel1", product_id=BID, cohort="__default__",
                      percent=100, account_id=ACCT)
    storage = LocalArtifactStorage(str(tmp_path / "blobs"))
    storage.put("m/rel1", b"MANI", "application/octet-stream")
    storage.put("i/rel1", b"IMG", "application/gzip")
    settings = ServerSettings(base_url="https://ota.test", swd_ids_verify_url="u",
                              swd_ids_verify_token="t", poll_jitter=0,
                              live_relay_url="https://live.test", live_token_secret="s3cret")
    return TestClient(create_app(settings, storage=storage, metastore=store,
                                 verifier=_Verifier())), store


def _register(store):
    return camera.register(store, DEV, ACCT)


def _body(dev="dev1"):
    return json.dumps({"device_id": dev, "product_id": BID, "payload_version": 0x01000000,
                       "board": "OPENMV_N6", "account_id": ACCT, "streams": ["0"]}).encode()


def _forged(body):
    return camera.sign(ec.generate_private_key(ec.SECP256R1()), body)


def _check(c, body, header=None):
    return camera.post(c, body, header).json()


def _audit(store, action):
    return [e for e in store.read_audit(account_id=ACCT) if e["action"] == action]


def test_a_signed_check_in_gets_everything_and_is_noted(tmp_path):
    c, store = _app(tmp_path)
    key = _register(store)
    body = _body()
    r = _check(c, body, camera.sign(key, body))
    assert r["update"] is True and "live" in r
    assert store.get_device(DEV) is not None
    assert store.get_registration(DEV)["last_signed_at"]


def test_bad_signatures_are_counted_and_audited_when_news(tmp_path):
    c, store = _app(tmp_path)
    key = _register(store)
    body = _body()
    forged = _forged(body)
    assert _check(c, body, forged) == NOTHING
    assert _check(c, body, forged) == NOTHING
    reg = store.get_registration(DEV)
    assert reg["refused_checkins"] == 2 and reg["last_refusal"] == "bad signature"
    assert reg["last_signed_at"] is None
    shown = c.get("/api/v1/admin/devices/%s/registration" % DEV,
                  headers={"Authorization": "Bearer tok"}).json()
    assert (shown["refused_checkins"], shown["last_refusal"]) == (2, "bad signature")
    refused = _audit(store, "device.checkin_refused")
    assert len(refused) == 1                                           # news once, then counted
    assert refused[0]["data"] == {"reason": "bad signature"}
    _check(c, body, camera.sign(key, body, ts=int(time.time()) - 3600))   # a different reason: news
    assert sorted(e["data"]["reason"] for e in _audit(store, "device.checkin_refused")) == \
        ["bad signature", "stale"]


def test_an_unsigned_check_in_gets_offers_only(tmp_path):
    c, store = _app(tmp_path)
    _register(store)
    r = _check(c, _body())                                             # a cold boot: no clock yet
    assert r["update"] is True and r["release_id"] == "rel1"
    assert "live" not in r and "ingest" not in r
    assert store.get_device(DEV) is None                               # no fleet row
    assert store.get_rollout("ro1")["attempted"] == 0
    other = json.loads(_body())
    other["product_id"] = BID + 1                                      # nothing to offer it
    assert _check(c, json.dumps(other).encode()) == NOTHING


def test_a_bad_signature_gets_nothing(tmp_path):
    c, store = _app(tmp_path)
    key = _register(store)
    body = _body()
    assert _check(c, body, _forged(body)) == NOTHING
    assert _check(c, body + b" ", camera.sign(key, body)) == NOTHING   # a changed body
    assert _check(c, body, "v=1;ts=1;sig=zz") == NOTHING
    assert store.get_device(DEV) is None
    assert store.get_registration(DEV)["refused_checkins"] == 3
    assert [e["data"] for e in _audit(store, "device.checkin_refused")] == \
        [{"reason": "bad signature"}, {"reason": "malformed"}]


def test_a_stale_signature_gets_offers_only(tmp_path):
    """A camera that rebooted with a drifted RTC signs its first check-in at the wrong time: it
    is counted, and answered as an unsigned check-in is -- not turned away."""
    c, store = _app(tmp_path)
    key = _register(store)
    body = _body()
    r = _check(c, body, camera.sign(key, body, ts=int(time.time()) - 3600))
    assert r["update"] is True and "live" not in r and "ingest" not in r
    assert store.get_device(DEV) is None
    assert store.get_registration(DEV)["last_refusal"] == "stale"


def test_a_signature_from_an_unregistered_camera_leaves_no_trace(tmp_path):
    c, store = _app(tmp_path)
    body = _body()
    assert _check(c, body, _forged(body)) == NOTHING
    assert store.get_device(DEV) is None and store.get_registration(DEV) is None
    assert _audit(store, "device.checkin_refused") == []               # nothing to hang it on

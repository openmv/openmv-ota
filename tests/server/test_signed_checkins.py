"""Signed check-ins on the server, and the ``require_signed_checkins`` switch.

A registered camera signs its check-in with the identity key its registration names. Off (the
default), signatures are checked and noted -- the good ones on the camera's registration, the bad
ones counted and audited -- and nothing else changes. On, only a signed check-in gets the full
treatment; an unsigned one gets update offers and nothing else, and a bad signature gets nothing.
"""

from __future__ import annotations

import json
import time

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient

from openmv_ota.ota import checkin_sig
from openmv_ota.server.app import create_app
from openmv_ota.server.auth import hash_token
from openmv_ota.server.metastore import SqliteMetadataStore
from openmv_ota.server.settings import ServerSettings
from openmv_ota.server.storage import LocalArtifactStorage
from openmv_ota.server.verify import Registration

BID = 7
ACCT = "acct_example"                 # a placeholder, not a real account
DEV = "OPENMV_N6:dev1"


class _Verifier:
    def verify(self, board, device_id):
        return Registration(True)


def _app(tmp_path, *, required):
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
                              live_relay_url="https://live.test", live_token_secret="s3cret",
                              require_signed_checkins=required)
    return TestClient(create_app(settings, storage=storage, metastore=store,
                                 verifier=_Verifier())), store


def _register(store, key, device_id=DEV):
    point = key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint).hex()
    store.put_registration(device_id, ACCT, record=b"{}", signature="00", identity=point,
                           exchange=point, desk_key_id=1, issued="2026-10-10T12:00:00Z",
                           registered_by="desk")


def _body(dev="dev1"):
    return json.dumps({"device_id": dev, "product_id": BID, "payload_version": 0x01000000,
                       "board": "OPENMV_N6", "account_id": ACCT, "streams": ["0"]}).encode()


def _sign(key, body, ts=None):
    ts = int(time.time()) if ts is None else ts
    sig = key.sign(checkin_sig.digest(body, ts), ec.ECDSA(Prehashed(hashes.SHA256())))
    return "v=1;ts=%d;sig=%s" % (ts, sig.hex())


def _check(c, body, header=None):
    headers = {"Content-Type": "application/json"}
    if header is not None:
        headers["X-OMV-Signature"] = header
    return c.post("/api/v1/check", content=body, headers=headers).json()


def _audit(store, action):
    return [e for e in store.read_audit(account_id=ACCT) if e["action"] == action]


def test_off_by_default():
    assert ServerSettings().require_signed_checkins is False


@pytest.mark.parametrize("required", [False, True])
def test_a_signed_check_in_gets_everything_and_is_noted(tmp_path, required):
    c, store = _app(tmp_path, required=required)
    key = ec.generate_private_key(ec.SECP256R1())
    _register(store, key)
    body = _body()
    r = _check(c, body, _sign(key, body))
    assert r["update"] is True and "live" in r
    assert store.get_device(DEV) is not None
    assert store.get_registration(DEV)["last_signed_at"]


def test_off_nothing_changes_but_bad_signatures_are_counted(tmp_path):
    c, store = _app(tmp_path, required=False)
    key = ec.generate_private_key(ec.SECP256R1())
    _register(store, key)
    body = _body()
    assert "live" in _check(c, body)                                   # unsigned: as today
    forged = _sign(ec.generate_private_key(ec.SECP256R1()), body)
    assert "live" in _check(c, body, forged)                           # observed, not enforced
    assert "live" in _check(c, body, forged)
    reg = store.get_registration(DEV)
    assert reg["refused_checkins"] == 2 and reg["last_refusal"] == "bad signature"
    assert reg["last_signed_at"] is None
    shown = c.get("/api/v1/admin/devices/%s/registration" % DEV,
                  headers={"Authorization": "Bearer tok"}).json()
    assert (shown["refused_checkins"], shown["last_refusal"]) == (2, "bad signature")
    refused = _audit(store, "device.checkin_refused")
    assert len(refused) == 1                                           # news once, then counted
    assert refused[0]["data"] == {"reason": "bad signature", "enforced": False}
    _check(c, body, _sign(key, body, ts=int(time.time()) - 3600))      # a different reason: news
    assert sorted(e["data"]["reason"] for e in _audit(store, "device.checkin_refused")) == \
        ["bad signature", "stale"]


def test_on_an_unsigned_check_in_gets_offers_only(tmp_path):
    c, store = _app(tmp_path, required=True)
    _register(store, ec.generate_private_key(ec.SECP256R1()))
    r = _check(c, _body())                                             # a cold boot: no clock yet
    assert r["update"] is True and r["release_id"] == "rel1"
    assert "live" not in r and "ingest" not in r
    assert store.get_device(DEV) is None                               # no fleet row
    assert store.get_rollout("ro1")["attempted"] == 0
    other = json.loads(_body())
    other["product_id"] = BID + 1                                      # nothing to offer it
    assert _check(c, json.dumps(other).encode()) == {"update": False, "poll_after_s": 3600}


def test_on_a_bad_signature_gets_nothing(tmp_path):
    c, store = _app(tmp_path, required=True)
    key = ec.generate_private_key(ec.SECP256R1())
    _register(store, key)
    body = _body()
    nothing = {"update": False, "poll_after_s": 3600}
    assert _check(c, body, _sign(ec.generate_private_key(ec.SECP256R1()), body)) == nothing
    assert _check(c, body + b" ", _sign(key, body)) == nothing         # a changed body
    assert _check(c, body, "v=1;ts=1;sig=zz") == nothing
    assert store.get_device(DEV) is None
    assert store.get_registration(DEV)["refused_checkins"] == 3
    assert _audit(store, "device.checkin_refused")[0]["data"]["enforced"] is True


def test_a_signature_from_an_unregistered_camera_leaves_no_trace(tmp_path):
    c, store = _app(tmp_path, required=True)
    body = _body()
    r = _check(c, body, _sign(ec.generate_private_key(ec.SECP256R1()), body))
    assert r == {"update": False, "poll_after_s": 3600}
    assert store.get_device(DEV) is None and store.get_registration(DEV) is None
    assert _audit(store, "device.checkin_refused") == []               # nothing to hang it on

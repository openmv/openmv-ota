"""Keykeeper's service: a lockbox only for a camera the SEAL's desk keys registered to the SEAL's
account -- whatever else the asking server says -- and every request audited. Its backup goes
only to the backup job's own token, and never with a plaintext key in it."""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from openmv_ota.keykeeper import ring as kr
from openmv_ota.keykeeper import service as ks
from openmv_ota.ota import lockbox as lb
from openmv_ota.ota import registration as reg
from openmv_ota.ota.algorithms import ES256, algorithm_for
from openmv_ota.ota.keys import generate_private_key, public_point_hex
from openmv_ota.ota.signer import LocalSigner

ALG = algorithm_for(ES256)
TOKEN = "kk-secret"
AUTH = {"Authorization": "Bearer " + TOKEN}


class _Clock:
    def __init__(self):
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def __call__(self):
        return self.now


@pytest.fixture
def kk(tmp_path):
    ring = kr.Ring(tmp_path / "kk", "pass")
    clock = _Clock()
    ring.init(clock.now, period_days=30)
    keeper = ks.Keykeeper(ring, TOKEN, tmp_path / "kk" / "audit.jsonl", period_days=30,
                          clock=clock)
    return keeper, TestClient(ks.create_app(keeper)), clock


def _desk():
    key = generate_private_key(ALG)
    expires = (datetime.now(timezone.utc) + timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return LocalSigner(key, 1, ES256, ALG), {"key_id": 1, "pubkey": public_point_hex(
        key.public_key()), "expires": expires, "revoked": False}


def _camera(desk_signer, account="acct_a"):
    exchange = ec.generate_private_key(ec.SECP256R1())
    record = reg.build(account=account, device_id="OPENMV_N6:01", board="OPENMV_N6",
                       identity=public_point_hex(generate_private_key(ALG).public_key()),
                       exchange=public_point_hex(exchange.public_key()), desk=1,
                       issued=datetime.now(timezone.utc))
    return exchange, record.decode(), reg.sign(record, desk_signer)


def _sealed(keeper, desks, *, account="acct_a", content_key=None, v=1):
    gen = kr.current(keeper.ring.generations(), keeper.clock())
    contents = {"v": v, "account": account, "content_key": (content_key or bytes(32)).hex(),
                "desks": desks, "release": "rel1"}
    return lb.seal(contents, gen.seal, gen.key_id)


def _audit(keeper):
    return [json.loads(line) for line in keeper.audit_path.read_text().splitlines()]


def test_a_registered_camera_gets_a_lockbox_only_it_opens(kk):
    keeper, c, _ = kk
    signer, desk = _desk()
    exchange, record, sig = _camera(signer)
    key = secrets.token_bytes(32)
    r = c.post("/v1/lockbox", headers=AUTH, json={"sealed": _sealed(keeper, [desk], content_key=key),
                                                  "record": record, "signature": sig})
    assert r.status_code == 200, r.text
    assert r.json()["device_id"] == "OPENMV_N6:01" and r.json()["seal_key_id"] == 1
    assert lb.open_lockbox(bytes.fromhex(r.json()["lockbox"]), exchange) == key
    event = _audit(keeper)[-1]
    assert event["ok"] and event["device_id"] == "OPENMV_N6:01" and event["release"] == "rel1"


def test_a_camera_the_seal_s_desks_didn_t_register_gets_nothing(kk):
    keeper, c, _ = kk
    signer, desk = _desk()
    rogue_signer, rogue_desk = _desk()                  # a desk the publisher never had
    _, record, sig = _camera(rogue_signer)
    post = lambda body: c.post("/v1/lockbox", headers=AUTH, json=body)  # noqa: E731
    r = post({"sealed": _sealed(keeper, [desk]), "record": record, "signature": sig})
    assert r.status_code == 422 and "doesn't match" in r.json()["detail"]
    _, theirs, their_sig = _camera(signer, account="acct_b")      # another account's camera
    r = post({"sealed": _sealed(keeper, [desk]), "record": theirs, "signature": their_sig})
    assert "another account" in r.json()["detail"]
    revoked = dict(desk, revoked=True)
    _, record, sig = _camera(signer)
    assert "revoked" in post({"sealed": _sealed(keeper, [revoked]), "record": record,
                              "signature": sig}).json()["detail"]
    assert [e["ok"] for e in _audit(keeper)] == [False, False, False]


@pytest.mark.parametrize("body, why", [
    ({"record": "x", "signature": "y"}, "is {sealed, record, signature}"),
    ({"sealed": [], "record": "x", "signature": "y"}, "is {sealed, record, signature}"),
    ({"sealed": {"v": 9, "key_id": 1}, "record": "x", "signature": "y"}, "version"),
    ({"sealed": {"v": 1, "key_id": 99, "eph": "", "nonce": "", "ct": ""}, "record": "x",
      "signature": "y"}, "no seal key for keykeeper generation 99"),
])
def test_bad_requests_say_why(kk, body, why):
    _, c, _ = kk
    r = c.post("/v1/lockbox", headers=AUTH, json=body)
    assert r.status_code == 422 and why in r.json()["detail"]


def test_bad_seal_contents_say_why(kk):
    keeper, c, _ = kk
    signer, desk = _desk()
    _, record, sig = _camera(signer)
    gen = kr.current(keeper.ring.generations(), keeper.clock())
    for contents, why in (({"v": 2}, "version"),
                          ({"v": 1, "account": ""}, "names no account"),
                          ({"v": 1, "account": "acct_a", "content_key": "zz"}, "content key"),
                          ({"v": 1, "account": "acct_a", "content_key": "00" * 32,
                            "desks": [{"key_id": "x"}]}, "desk keys are malformed")):
        sealed = lb.seal(contents, gen.seal, gen.key_id)
        r = c.post("/v1/lockbox", headers=AUTH,
                   json={"sealed": sealed, "record": record, "signature": sig})
        assert why in r.json()["detail"], contents


def test_only_the_server_with_the_secret_may_ask(kk, tmp_path):
    keeper, c, _ = kk
    assert c.post("/v1/lockbox", json={}).status_code == 401
    assert c.post("/v1/lockbox", headers={"Authorization": "Bearer nope"}, json={}).status_code \
        == 401
    assert c.get("/v1/chain").status_code == 401
    assert c.get("/v1/chain", headers=AUTH).json()["chain"] == keeper.ring.chain()
    assert c.get("/healthz").json() == {"ok": True}
    assert c.post("/v1/lockbox", headers=AUTH, content=b"not json").status_code == 422
    assert c.post("/v1/lockbox", headers=AUTH, json=[1]).status_code == 422
    with pytest.raises(kr.KeykeeperError, match="shared secret"):
        ks.Keykeeper(keeper.ring, "", tmp_path / "a.jsonl")


def test_a_running_keykeeper_keeps_the_next_generation_announced(kk, monkeypatch):
    keeper, c, clock = kk
    clock.now += timedelta(days=31)                       # generation 2 is now in effect
    monkeypatch.setattr(ks, "_EXTEND_EVERY_S", -1)        # check on every request
    chain = c.get("/v1/chain", headers=AUTH).json()["chain"]
    assert len(chain) == 3                                # and generation 3 is announced
    signer, desk = _desk()
    exchange, record, sig = _camera(signer)
    sealed = _sealed(keeper, [desk])
    assert sealed["key_id"] == 2
    r = c.post("/v1/lockbox", headers=AUTH,
               json={"sealed": sealed, "record": record, "signature": sig})
    assert r.json()["seal_key_id"] == 2


BACKUP_TOKEN = "kk-backup-secret"
BACKUP_AUTH = {"Authorization": "Bearer " + BACKUP_TOKEN}


@pytest.fixture
def kkb(kk):
    keeper, _, clock = kk
    keeper = ks.Keykeeper(keeper.ring, TOKEN, keeper.audit_path, period_days=30, clock=clock,
                          backup_token=BACKUP_TOKEN)
    return keeper, TestClient(ks.create_app(keeper))


def test_the_backup_route_exists_only_with_a_backup_token(kk):
    keeper, c, _ = kk
    assert c.get("/v1/backup").status_code == 404
    assert c.get("/v1/backup", headers=AUTH).status_code == 404
    assert c.get("/v1/backup", headers={"Authorization": "Bearer "}).status_code == 404
    assert not keeper.backup_authorized("Bearer ")              # an empty secret opens nothing


def test_the_backup_token_fetches_the_backup_and_nothing_else(kkb):
    keeper, c = kkb
    r = c.get("/v1/backup", headers=BACKUP_AUTH)
    assert r.status_code == 200 and r.headers["content-type"] == "application/gzip"
    assert r.content == keeper.ring.backup()
    assert r.headers["x-keykeeper-backup-sha256"] == hashlib.sha256(r.content).hexdigest()
    assert r.headers["cache-control"] == "no-store"
    event = _audit(keeper)[-1]
    assert event["action"] == "backup" and event["ok"] and event["bytes"] == len(r.content)
    assert event["sha256"] == r.headers["x-keykeeper-backup-sha256"]
    assert c.get("/v1/backup").status_code == 401
    assert c.get("/v1/backup", headers=AUTH).status_code == 401           # the server's token
    assert c.get("/v1/chain", headers=BACKUP_AUTH).status_code == 401
    assert c.post("/v1/lockbox", headers=BACKUP_AUTH, json={}).status_code == 401
    assert c.get("/v1/chain", headers=AUTH).status_code == 200


def test_a_plaintext_key_on_disk_stops_the_backup(kkb, caplog):
    keeper, c = kkb
    seal = keeper.ring.root / "private" / "kk-0001-seal.pem"
    seal.write_bytes(keeper.ring.seal_key(1).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    with caplog.at_level(logging.ERROR, logger="openmv_ota.keykeeper"):
        r = c.get("/v1/backup", headers=BACKUP_AUTH)
    assert r.status_code == 500 and "kk-0001-seal.pem is not an encrypted key" in r.text
    assert "BEGIN PRIVATE KEY" not in r.text
    assert "refused a backup" in caplog.text
    event = _audit(keeper)[-1]
    assert event["action"] == "backup" and not event["ok"] and "not an encrypted" in event["reason"]


def test_the_backup_token_can_t_be_the_server_s(kk, tmp_path):
    keeper, _, _ = kk
    with pytest.raises(kr.KeykeeperError, match="must not be the server's token"):
        ks.Keykeeper(keeper.ring, TOKEN, tmp_path / "a.jsonl", backup_token=TOKEN)

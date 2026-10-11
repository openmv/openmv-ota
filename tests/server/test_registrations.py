"""Desk keys and camera registrations on the server: what makes a camera's keys the account's.

A token alone registers nothing: a record must carry a desk key's signature, from a key the
account recorded, unrevoked and unexpired, issued just now. And one account can't take another's
camera by registering it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from openmv_ota.ota import registration as reg
from openmv_ota.ota.algorithms import ES256, algorithm_for
from openmv_ota.ota.keys import generate_private_key, public_point_hex
from openmv_ota.ota.signer import LocalSigner
from openmv_ota.server.app import create_app
from openmv_ota.server.auth import hash_token
from openmv_ota.server.metastore import SqliteMetadataStore
from openmv_ota.server.settings import ServerSettings
from openmv_ota.server.storage import LocalArtifactStorage
from openmv_ota.server.verify import Registration

ALG = algorithm_for(ES256)
A = {"Authorization": "Bearer tokA"}
B = {"Authorization": "Bearer tokB"}
DESK = {"Authorization": "Bearer tokD"}           # acctA's factory: register, nothing else
OBSERVER = {"Authorization": "Bearer tokO"}       # acctA, observe only


class _Verifier:
    def verify(self, board, device_id):
        return Registration(True)


def _server(tmp_path):
    store = SqliteMetadataStore(str(tmp_path / "ota.db"))
    store.migrate()
    store.set_meta("capability_secret", "x")
    for acc in ("acctA", "acctB"):
        store.add_account(acc, acc)
        store.add_token(hash_token("tok" + acc[-1]), acc, ["publish", "manage", "observe"],
                        account_id=acc)
    store.add_token(hash_token("tokD"), "factory-1", ["register"], account_id="acctA")
    store.add_token(hash_token("tokO"), "viewer", ["observe"], account_id="acctA")
    app = create_app(ServerSettings(base_url="https://ota.test", swd_ids_verify_url="u",
                                    swd_ids_verify_token="t"),
                     metastore=store, storage=LocalArtifactStorage(str(tmp_path / "blobs")),
                     verifier=_Verifier())
    return TestClient(app), store


def _desk(key_id=1):
    key = generate_private_key(ALG)
    return LocalSigner(key, key_id, ES256, ALG), public_point_hex(key.public_key())


def _expires(days=90):
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _push(c, headers, key_id, pub, *, expires=None, revoked=False, label="bench-1"):
    return c.put("/api/v1/admin/desk-keys/%d" % key_id, headers=headers,
                  json={"label": label, "pubkey": pub, "expires": expires or _expires(),
                        "revoked": revoked})


def _record(signer, *, account="acctA", device="OPENMV4:01", board="OPENMV4", identity=None,
            issued=None):
    identity = identity or public_point_hex(generate_private_key(ALG).public_key())
    record = reg.build(account=account, device_id=device, board=board, identity=identity,
                       exchange=public_point_hex(generate_private_key(ALG).public_key()),
                       desk=signer.key_id, issued=issued or datetime.now(timezone.utc))
    return {"record": record.decode(), "signature": reg.sign(record, signer)}, identity


def test_an_account_records_its_desk_keys(tmp_path):
    c, _store = _server(tmp_path)
    _signer, pub = _desk()
    r = _push(c, A, 1, pub)
    assert r.status_code == 200 and r.json()["pubkey"] == pub and not r.json()["revoked"]
    listed = c.get("/api/v1/admin/desk-keys", headers=OBSERVER).json()["desk_keys"]
    assert [k["key_id"] for k in listed] == [1]
    assert c.get("/api/v1/admin/desk-keys", headers=B).json()["desk_keys"] == []   # per account
    assert _push(c, A, 1, pub, expires=_expires(180)).status_code == 200            # renewed
    _other, pub2 = _desk()
    assert _push(c, A, 1, pub2).status_code == 409                     # id's key is fixed
    assert _push(c, A, 1, pub, revoked=True).json()["revoked"]
    assert _push(c, A, 1, pub, revoked=False).json()["revoked"]        # revoking is one-way
    assert _push(c, A, 2, "04abcd").status_code == 400
    assert _push(c, A, 2, pub2, expires="2027-01-01T00:00:00").status_code == 400   # no zone
    assert _push(c, A, 2, pub2, expires="soon").status_code == 400
    assert _push(c, DESK, 3, pub2).status_code == 403                  # a desk can't add keys
    actions = [e["action"] for e in c.get("/api/v1/admin/audit", headers=A).json()["events"]]
    assert "desk_key.put" in actions


def test_a_desk_signed_record_registers_and_binds_the_camera(tmp_path):
    c, store = _server(tmp_path)
    signer, pub = _desk()
    _push(c, A, 1, pub)
    body, identity = _record(signer)
    r = c.post("/api/v1/admin/registrations", headers=DESK, json=body)
    assert r.status_code == 200, r.text
    got = r.json()
    assert (got["device_id"], got["account_id"], got["rekeyed"]) == ("OPENMV4:01", "acctA", False)
    assert store.device_account("OPENMV4:01") == {"account_id": "acctA", "source": "admin"}
    row = c.get("/api/v1/admin/devices/OPENMV4:01/registration", headers=OBSERVER).json()
    assert row["identity"] == identity and row["fingerprint"] == got["fingerprint"]
    assert row["record"] == body["record"] and row["registered_by"] == "factory-1"
    # the stored record still verifies: the exact bytes came back
    assert reg.verify(row["record"].encode(), row["signature"],
                      [reg.DeskKey(1, pub, _expires())]).identity == identity
    assert c.get("/api/v1/admin/devices/OPENMV4:01/registration", headers=B).status_code == 404


def test_registering_again_with_new_keys_is_a_rekey(tmp_path):
    c, _store = _server(tmp_path)
    signer, pub = _desk()
    _push(c, A, 1, pub)
    first, identity = _record(signer)
    c.post("/api/v1/admin/registrations", headers=DESK, json=first)
    same, _ = _record(signer, identity=identity)
    assert not c.post("/api/v1/admin/registrations", headers=DESK, json=same).json()["rekeyed"]
    fresh, _ = _record(signer)
    assert c.post("/api/v1/admin/registrations", headers=DESK, json=fresh).json()["rekeyed"]
    events = c.get("/api/v1/admin/audit", headers=A).json()["events"]
    rekey = [e for e in events if e["action"] == "device.rekeyed"]
    assert len(rekey) == 1 and "previous_fingerprint" in rekey[0]["data"]


def test_a_token_alone_registers_nothing(tmp_path):
    c, store = _server(tmp_path)
    signer, pub = _desk()
    body, _ = _record(signer)
    post = lambda b, h=DESK: c.post("/api/v1/admin/registrations", headers=h, json=b)  # noqa: E731
    assert post(body).status_code == 400                       # the account has no such desk key
    _push(c, A, 1, pub)
    forged = dict(body, signature="00" * 64)
    assert post(forged).status_code == 400
    stale, _ = _record(signer, issued=datetime.now(timezone.utc) - timedelta(hours=3))
    assert "issued just now" in post(stale).json()["detail"]
    theirs, _ = _record(signer, account="acctB")
    assert "another account" in post(theirs).json()["detail"]
    mislabelled, _ = _record(signer, device="OPENMV_N6:01")
    assert "isn't the board's" in post(mislabelled).json()["detail"]
    assert post(body, OBSERVER).status_code == 403             # observe can't register
    _push(c, A, 1, pub, revoked=True)
    assert "revoked" in post(body).json()["detail"]
    assert store.get_registration("OPENMV4:01") is None
    refused = [e for e in c.get("/api/v1/admin/audit", headers=A).json()["events"]
               if e["action"] == "device.register_refused"]
    assert len(refused) == 6                                  # the 403 never reached the handler


def test_an_expired_desk_key_cant_register(tmp_path):
    c, _store = _server(tmp_path)
    signer, pub = _desk()
    _push(c, A, 1, pub, expires=(datetime.now(timezone.utc) - timedelta(minutes=1))
          .strftime("%Y-%m-%dT%H:%M:%SZ"))
    body, _ = _record(signer, issued=datetime.now(timezone.utc) - timedelta(minutes=10))
    assert "has expired" in c.post("/api/v1/admin/registrations", headers=DESK,
                                   json=body).json()["detail"]


def test_one_account_cant_take_another_s_camera(tmp_path):
    c, store = _server(tmp_path)
    sig_a, pub_a = _desk()
    sig_b, pub_b = _desk()
    _push(c, A, 1, pub_a)
    _push(c, B, 1, pub_b)
    body_a, _ = _record(sig_a)
    assert c.post("/api/v1/admin/registrations", headers=A, json=body_a).status_code == 200
    body_b, _ = _record(sig_b, account="acctB")
    assert c.post("/api/v1/admin/registrations", headers=B, json=body_b).status_code == 404
    assert store.get_registration("OPENMV4:01")["account_id"] == "acctA"
    # nor one acctA has admin-bound but never registered
    store.bind_device_account("OPENMV4:02", "acctA", source="admin")
    body_b2, _ = _record(sig_b, account="acctB", device="OPENMV4:02")
    assert c.post("/api/v1/admin/registrations", headers=B, json=body_b2).status_code == 404


def test_registrations_survive_forget_and_go_with_the_account(tmp_path):
    c, store = _server(tmp_path)
    signer, pub = _desk()
    _push(c, A, 1, pub)
    body, _ = _record(signer)
    c.post("/api/v1/admin/registrations", headers=A, json=body)
    store.upsert_device(device_id="OPENMV4:01", product_id=7, account_id="acctA")
    assert c.delete("/api/v1/admin/devices/OPENMV4:01", headers=A).status_code == 200
    assert store.get_registration("OPENMV4:01") is not None    # the camera is still ours
    assert store.registrations_for(["OPENMV4:01", "nope"]).keys() == {"OPENMV4:01"}
    assert store.registrations_for([]) == {}
    store.set_account_active("acctA", False)
    store.delete_account("acctA")
    assert store.get_registration("OPENMV4:01") is None and store.desk_keys("acctA") == []

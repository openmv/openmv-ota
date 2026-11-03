"""An account's pinned roots: set once by the project, replaced only under a root-signed statement,
and the anchor every desk key's certificate chain must reach."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from openmv_ota.ota import account_roots as ar
from openmv_ota.ota import certificate as cert_mod
from openmv_ota.ota.errors import OtaError
from openmv_ota.ota.keys import generate_private_key, public_point_hex
from openmv_ota.server.app import create_app
from openmv_ota.server.auth import hash_token
from openmv_ota.server.metastore import SqliteMetadataStore
from openmv_ota.server.settings import ServerSettings
from openmv_ota.server.storage import LocalArtifactStorage
from tests.server.trust import ALG, Chain

A = {"Authorization": "Bearer tokA"}
OBSERVER = {"Authorization": "Bearer tokO"}


def _server(tmp_path):
    store = SqliteMetadataStore(str(tmp_path / "ota.db"))
    store.migrate()
    store.set_meta("capability_secret", "x")
    store.add_account("acctA", "acctA")
    store.add_token(hash_token("tokA"), "acctA", ["publish", "manage", "observe"],
                    account_id="acctA")
    store.add_token(hash_token("tokO"), "viewer", ["observe"], account_id="acctA")
    app = create_app(ServerSettings(base_url="https://ota.test", swd_ids_verify_url="u",
                                    swd_ids_verify_token="t"),
                     metastore=store, storage=LocalArtifactStorage(str(tmp_path / "blobs")))
    return TestClient(app), store


def _actions(c):
    return [e["action"] for e in c.get("/api/v1/admin/audit", headers=A).json()["events"]]


def test_a_fresh_account_takes_its_first_roots_and_then_only_the_same_ones(tmp_path):
    c, store = _server(tmp_path)
    chain = Chain()
    assert c.get("/api/v1/admin/roots", headers=OBSERVER).json()["roots"] == []
    r = chain.pin(c, A)
    assert r.status_code == 200 and not r.json()["replaced"]
    got = r.json()["roots"]
    assert [(x["root_id"], x["pubkey"]) for x in got] == [(0, chain.roots[0]["pubkey"]),
                                                          (1, chain.roots[1]["pubkey"])]
    assert all(x["set_at"] for x in got)
    assert chain.pin(c, A).status_code == 200                         # the same set: a no-op
    assert c.get("/api/v1/admin/roots", headers=OBSERVER).json()["roots"] == got
    other = Chain()
    r = other.pin(c, A)                                               # a stranger's roots
    assert r.status_code == 409 and "statement signed by a current root" in r.json()["detail"]
    assert chain.pin(c, OBSERVER).status_code == 403                  # observe can't pin
    assert _actions(c).count("account.roots_pinned") == 1 and "account.roots_replaced" \
        not in _actions(c)


def test_replacing_the_roots_takes_a_current_root_s_statement(tmp_path):
    c, _store = _server(tmp_path)
    old, new = Chain(), Chain()
    assert old.pin(c, A).status_code == 200
    statement = ar.sign_statement("acctA", new.roots, old.root_signer(1))
    r = new.pin(c, A, statement=statement)
    assert r.status_code == 200 and r.json()["replaced"]
    assert [x["pubkey"] for x in c.get("/api/v1/admin/roots", headers=A).json()["roots"]] == \
        [x["pubkey"] for x in new.roots]
    replaced = [e for e in c.get("/api/v1/admin/audit", headers=A).json()["events"]
                if e["action"] == "account.roots_replaced"]
    assert len(replaced) == 1 and replaced[0]["data"]["signed_by"] == 1
    assert len(replaced[0]["data"]["roots"]) == 2 and "fingerprint" in replaced[0]["data"]["roots"][0]
    # the old roots are gone: a statement by old root 0 fails against NEW root 0
    third = Chain()
    stale = ar.sign_statement("acctA", third.roots, old.root_signer(0))
    r = third.pin(c, A, statement=stale)
    assert r.status_code == 400 and "not signed by root 0" in r.json()["detail"]
    unknown = dict(stale, signed_by=5)
    r = third.pin(c, A, statement=unknown)
    assert r.status_code == 400 and "not one of the account's current roots" in r.json()["detail"]
    # a statement by a current root over DIFFERENT roots than the body carries does not verify
    wrong = ar.sign_statement("acctA", old.roots, new.root_signer(0))
    assert "not signed by root 0" in third.pin(c, A, statement=wrong).json()["detail"]
    # and a statement for another account is not this one's
    elsewhere = ar.sign_statement("acctB", third.roots, new.root_signer(0))
    assert third.pin(c, A, statement=elsewhere).status_code == 400
    assert third.pin(c, A, statement={"signed_by": "x"}).status_code == 400
    assert third.pin(c, A, statement={"signed_by": 0, "signature": "zz"}).status_code == 400


def test_malformed_roots_are_refused(tmp_path):
    c, _store = _server(tmp_path)
    put = lambda roots: c.put("/api/v1/admin/roots", headers=A, json={"roots": roots})  # noqa: E731
    good = public_point_hex(generate_private_key(ALG).public_key())
    assert put([]).status_code == 400
    assert put([{"root_id": i, "pubkey": good} for i in range(5)]).status_code == 400
    assert put([{"root_id": 0}]).status_code == 400
    assert put([{"root_id": 0, "pubkey": "04abcd"}]).status_code == 400
    assert put([{"root_id": 0, "pubkey": "03" + good[2:]}]).status_code == 400   # compressed
    assert put([{"root_id": 0, "pubkey": good}, {"root_id": 0, "pubkey": good}]).status_code == 400
    assert put([{"root_id": 99, "pubkey": good}]).status_code == 400
    assert put([{"root_id": True, "pubkey": good}]).status_code == 400
    assert put([{"root_id": 1, "pubkey": good.upper()}]).status_code == 200    # case-folded
    assert c.get("/api/v1/admin/roots", headers=A).json()["roots"][0]["pubkey"] == good


def test_a_desk_key_is_taken_only_through_a_chain_to_the_pinned_roots(tmp_path):
    c, store = _server(tmp_path)
    chain = Chain()
    signer, pub = chain.desk()
    r = chain.push(c, A, pub)
    assert r.status_code == 400 and "no pinned roots yet" in r.json()["detail"]
    assert chain.pin(c, A).status_code == 200
    assert chain.push(c, A, pub).status_code == 200
    # another project's chain, however well-formed, does not end at these roots
    stranger = Chain()
    _s2, pub2 = stranger.desk()
    r = stranger.push(c, A, pub2)
    assert r.status_code == 400 and "chain does not verify" in r.json()["detail"]
    # a desk certificate for a different key than the one pushed
    _s3, pub3 = chain.desk()
    body = chain.body(pub3)
    body["cert"] = chain.certs[pub]                      # someone else's certificate
    r = c.put("/api/v1/admin/desk-keys/%d" % cert_mod.key_id_of(bytes.fromhex(pub3)),
              headers=A, json=body)
    assert r.status_code == 400 and "different key" in r.json()["detail"]
    # an uncertified push (empty certificates) is refused, not silently taken
    body = chain.body(pub3) | {"cert": "", "issuer_cert": ""}
    r = c.put("/api/v1/admin/desk-keys/%d" % cert_mod.key_id_of(bytes.fromhex(pub3)),
              headers=A, json=body)
    assert r.status_code == 400
    body = chain.body(pub3) | {"cert": "zz"}
    assert c.put("/api/v1/admin/desk-keys/%d" % cert_mod.key_id_of(bytes.fromhex(pub3)),
                 headers=A, json=body).status_code == 400
    refused = [e for e in c.get("/api/v1/admin/audit", headers=A).json()["events"]
               if e["action"] == "desk_key.refused"]
    assert len(refused) == 4 and all(e["data"]["reason"] for e in refused)
    assert [k["key_id"] for k in store.desk_keys("acctA")] == [signer.key_id]


def test_the_statement_codec_is_exact_bytes():
    chain = Chain()
    region = ar.statement_bytes("acctA", chain.roots)
    assert region.startswith(b'{"account":"acctA","roots":[{"pubkey":"04')
    assert ar.statement_bytes("acctA", list(reversed(chain.roots))) == region   # sorted by id
    st = ar.sign_statement("acctA", chain.roots, chain.root_signer(0))
    assert ar.verify_statement("acctA", chain.roots, st, chain.roots) == ar.check_roots(chain.roots)
    try:
        ar.check_roots("nope")
    except OtaError as e:
        assert "between 1 and" in str(e)


def test_the_store_keeps_a_recorded_key_s_public_half_fixed(tmp_path):
    _c, store = _server(tmp_path)
    store.put_desk_key("acctA", 5, label="b", pubkey="04aa", expires="2099-01-01T00:00:00Z",
                       revoked=False, cert="aa", issuer_cert="bb")
    with pytest.raises(ValueError, match="different public key"):
        store.put_desk_key("acctA", 5, label="b", pubkey="04bb", expires="2099-01-01T00:00:00Z",
                           revoked=False)
    assert store.desk_keys("acctA")[0]["cert"] == "aa"

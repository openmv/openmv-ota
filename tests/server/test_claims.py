"""Claiming a board: registering is how an account makes a camera its own.

A board type the registrar never registers (the Arduino boards) is a registered device once the
account it checks in under holds a desk-signed registration for it. Until then the server keeps a
bounded list of the account's unclaimed boards, so the account can see what to claim. Claiming is
bounded (known boards, the device limit, a rate), and an account can release a camera it holds --
the server's root token any account's, which is how a disputed board is settled.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from openmv_ota.ota import registration as reg
from openmv_ota.ota.algorithms import ES256, algorithm_for
from openmv_ota.ota.keys import generate_private_key, public_point_hex
from openmv_ota.ota.signer import LocalSigner
from openmv_ota.server import metastore as M
from openmv_ota.server.app import create_app
from openmv_ota.server.auth import hash_token
from openmv_ota.server.metastore import SqliteMetadataStore
from openmv_ota.server.settings import ServerSettings
from openmv_ota.server.storage import LocalArtifactStorage
from openmv_ota.server.verify import Registration

ALG = algorithm_for(ES256)
BID = 7
A = {"Authorization": "Bearer tokA"}
B = {"Authorization": "Bearer tokB"}
ROOT = {"Authorization": "Bearer tokR"}
GIGA = "ARDUINO_GIGA:dev1"


class _Registry:
    """The registrar's verdict: OpenMV boards are registered, Arduino boards never are."""

    def verify(self, board, device_id):
        if board.startswith("ARDUINO_"):
            return Registration(False, unregistered_board_type=True)
        return Registration(True)


def _server(tmp_path, *, rate=30, serve_unregistered=False):
    store = SqliteMetadataStore(str(tmp_path / "ota.db"))
    store.migrate()
    store.set_meta("capability_secret", "x")
    for acc in ("acctA", "acctB"):
        store.add_account(acc, acc)
        store.add_token(hash_token("tok" + acc[-1]), acc,
                        ["publish", "manage", "observe", "register"], account_id=acc)
    store.add_token(hash_token("tokR"), "root", ["publish", "accounts.all"])
    settings = ServerSettings(base_url="https://ota.test", swd_ids_verify_url="u",
                              swd_ids_verify_token="t", poll_jitter=0,
                              registration_rate_per_min=rate,
                              serve_unregistered_boards=serve_unregistered)
    app = create_app(settings, metastore=store,
                     storage=LocalArtifactStorage(str(tmp_path / "blobs")), verifier=_Registry())
    return TestClient(app), store


def _desk(c, headers, key_id=1):
    key = generate_private_key(ALG)
    pub = public_point_hex(key.public_key())
    expires = datetime(2099, 1, 1, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert c.put("/api/v1/admin/desk-keys/%d" % key_id, headers=headers,
                 json={"label": "bench", "pubkey": pub, "expires": expires}).status_code == 200
    return LocalSigner(key, key_id, ES256, ALG)


def _register(c, headers, signer, *, account="acctA", device=GIGA, board="ARDUINO_GIGA"):
    record = reg.build(account=account, device_id=device, board=board,
                       identity=public_point_hex(generate_private_key(ALG).public_key()),
                       exchange=public_point_hex(generate_private_key(ALG).public_key()),
                       desk=signer.key_id, issued=datetime.now(timezone.utc))
    return c.post("/api/v1/admin/registrations", headers=headers,
                  json={"record": record.decode(), "signature": reg.sign(record, signer)})


def _check(c, dev="dev1", board="ARDUINO_GIGA", account="acctA"):
    return c.post("/api/v1/check", json={"device_id": dev, "product_id": BID,
                                         "payload_version": 0x01000000, "board": board,
                                         "account_id": account, "streams": ["0"]})


def _actions(c, headers):
    return [e["action"] for e in c.get("/api/v1/admin/audit", headers=headers).json()["events"]]


def test_an_unclaimed_board_is_listed_for_its_account_and_served_read_only(tmp_path):
    c, store = _server(tmp_path)
    assert _check(c).status_code == 200
    assert store.get_device(GIGA) is None                       # no fleet row until claimed
    got = c.get("/api/v1/admin/unclaimed", headers=A).json()
    assert got["total"] == 1
    row = got["devices"][0]
    assert (row["device_id"], row["board"], row["product_id"], row["product_id_str"]) == \
        (GIGA, "ARDUINO_GIGA", BID, str(BID))
    assert c.get("/api/v1/admin/unclaimed", headers=B).json() == {"devices": [], "total": 0}
    _check(c, account="no-such-account")                        # nobody to show it to
    _check(c, dev="dev2", account="acctB")
    assert c.get("/api/v1/admin/unclaimed", headers=ROOT).json()["total"] == 2   # every account
    assert _check(c, dev="dev3", board="OPENMV4").status_code == 200   # registered: not listed
    assert c.get("/api/v1/admin/unclaimed", headers=A).json()["total"] == 1


def test_a_claimed_board_is_a_registered_device_of_its_account_only(tmp_path):
    c, store = _server(tmp_path)
    _check(c)
    assert _register(c, A, _desk(c, A)).status_code == 200
    assert c.get("/api/v1/admin/unclaimed", headers=A).json()["total"] == 0   # claimed
    _check(c)
    assert store.get_device(GIGA)["account_id"] == "acctA"     # tracked like any camera
    store.forget_device(GIGA)                                   # leaves the registration
    _check(c, account="acctB")                                  # the claim is acctA's
    assert store.get_device(GIGA) is None
    assert c.get("/api/v1/admin/unclaimed", headers=B).json()["total"] == 1


def test_the_pre_launch_switch_still_serves_every_arduino_board(tmp_path):
    c, store = _server(tmp_path, serve_unregistered=True)
    _check(c)
    assert store.get_device(GIGA)["account_id"] == "acctA"
    assert c.get("/api/v1/admin/unclaimed", headers=A).json()["total"] == 0


def test_a_registered_camera_keeps_its_seat_at_check_in(tmp_path):
    c, store = _server(tmp_path)
    store.set_device_limit("acctA", 1)
    assert _register(c, A, _desk(c, A)).status_code == 200    # took the only seat
    assert store.claimed_count("acctA") == 1 and store.over_device_limit("acctA")
    _check(c)
    assert store.get_device(GIGA)["account_id"] == "acctA"     # not refused at its first check-in
    _check(c, dev="dev2", board="OPENMV4")                      # but a second camera is
    assert store.get_device("OPENMV4:dev2") is None
    assert "device.refused" in _actions(c, A)


def test_registering_a_new_camera_past_the_limit_is_refused(tmp_path):
    c, store = _server(tmp_path)
    signer = _desk(c, A)
    store.set_device_limit("acctA", 1)
    assert _register(c, A, signer).status_code == 200
    assert _register(c, A, signer).status_code == 200          # the same camera, re-keyed
    r = _register(c, A, signer, device="ARDUINO_GIGA:dev2")
    assert r.status_code == 403 and "device limit" in r.json()["detail"]
    assert store.get_registration("ARDUINO_GIGA:dev2") is None
    assert "device.register_refused" in _actions(c, A)
    store.upsert_device(device_id="OPENMV4:fleet", product_id=BID, account_id="acctA")
    store.set_device_limit("acctA", 2)                          # a fleet camera registering
    assert _register(c, A, signer, device="OPENMV4:fleet", board="OPENMV4").status_code == 200


def test_only_a_known_board_can_be_claimed(tmp_path):
    c, store = _server(tmp_path)
    r = _register(c, A, _desk(c, A), device="TOASTER:1", board="TOASTER")
    assert r.status_code == 400 and "unknown board" in r.json()["detail"]
    assert store.get_registration("TOASTER:1") is None


def test_registering_is_rate_limited(tmp_path):
    c, _store = _server(tmp_path, rate=2)
    signer = _desk(c, A)
    assert [_register(c, A, signer).status_code for _ in range(3)] == [200, 200, 429]
    signer_b = _desk(c, B)                                       # the address is spent too
    assert _register(c, B, signer_b, account="acctB", device="ARDUINO_GIGA:b").status_code == 429


def test_an_account_releases_a_camera_it_holds(tmp_path):
    c, store = _server(tmp_path)
    _register(c, A, _desk(c, A))
    _check(c)
    assert c.delete("/api/v1/admin/devices/%s/registration" % GIGA, headers=B).status_code == 404
    r = c.delete("/api/v1/admin/devices/%s/registration" % GIGA, headers=A)
    assert r.status_code == 200 and r.json() == {"device_id": GIGA, "account_id": "acctA",
                                                 "released": True}
    assert store.get_registration(GIGA) is None and store.get_device(GIGA) is None
    assert store.device_account(GIGA) is None
    assert "device.released" in _actions(c, A)
    assert c.delete("/api/v1/admin/devices/%s/registration" % GIGA, headers=A).status_code == 404
    _check(c)                                                    # unclaimed again
    assert c.get("/api/v1/admin/unclaimed", headers=A).json()["total"] == 1
    assert _register(c, B, _desk(c, B), account="acctB").status_code == 200   # B can claim it


def test_root_settles_a_disputed_camera(tmp_path):
    c, store = _server(tmp_path)
    _register(c, A, _desk(c, A))
    r = c.delete("/api/v1/admin/devices/%s/registration" % GIGA, headers=ROOT)
    assert r.status_code == 200 and r.json()["account_id"] == "acctA"
    assert store.get_registration(GIGA) is None


def test_the_unclaimed_list_is_bounded_per_account(tmp_path, monkeypatch):
    store = SqliteMetadataStore(str(tmp_path / "ota.db"))
    store.migrate()
    monkeypatch.setattr(M, "UNCLAIMED_CAP", 2)
    times = iter("2026-01-01T00:00:0%d+00:00" % i for i in range(9))
    monkeypatch.setattr(M, "_now_iso", lambda: next(times))
    for dev in ("a", "b", "c"):
        store.note_unclaimed(dev, "acct", "ARDUINO_GIGA", None)
    store.note_unclaimed("x", "other", "ARDUINO_GIGA", 9)
    rows, total = store.unclaimed("acct", 50, 0)
    assert total == 2 and [r["device_id"] for r in rows] == ["c", "b"]   # the oldest went
    store.note_unclaimed("b", "acct", "ARDUINO_NICLA_VISION", 3)          # seen again
    rows, _ = store.unclaimed("acct", 1, 0)
    assert rows[0]["device_id"] == "b" and rows[0]["board"] == "ARDUINO_NICLA_VISION"
    assert rows[0]["first_seen"] != rows[0]["last_seen"]
    assert [r["device_id"] for r in store.unclaimed("acct", 1, 1)[0]] == ["c"]
    assert store.unclaimed(None, 50, 0)[1] == 3
    store.drop_unclaimed("b")
    assert store.unclaimed(None, 50, 0)[1] == 2
    assert store.delete_registration("nope") is None


def test_a_camera_registered_elsewhere_can_t_be_bound(tmp_path):
    c, store = _server(tmp_path)
    _register(c, A, _desk(c, A))
    _check(c)
    assert c.delete("/api/v1/admin/devices/%s" % GIGA, headers=A).status_code == 200   # forgotten
    assert c.post("/api/v1/admin/devices/%s/account" % GIGA, headers=B).status_code == 404
    assert c.post("/api/v1/admin/devices/%s/account" % GIGA, headers=A).status_code == 200
    c.delete("/api/v1/admin/devices/%s/registration" % GIGA, headers=A)               # released
    assert c.post("/api/v1/admin/devices/%s/account" % GIGA, headers=B).status_code == 200

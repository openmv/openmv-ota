"""Offering a release encrypted for each camera: the offer carries the camera's lockbox from
keykeeper, and a camera that can't get one -- no keykeeper, no registration, a refusal -- isn't
offered the release at all (it couldn't open it)."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from openmv_ota.server import keykeeper as kk_mod
from openmv_ota.server.app import create_app
from openmv_ota.server.auth import hash_token
from openmv_ota.server.metastore import SqliteMetadataStore
from openmv_ota.server.settings import ServerSettings
from openmv_ota.server.storage import LocalArtifactStorage
from openmv_ota.server.verify import Registration

BID = 7
ACCT = "acct_example"                 # a placeholder, not a real account
DEV = "OPENMV_N6:dev1"
SEALED = {"v": 1, "key_id": 1, "eph": "04", "nonce": "00", "ct": "00"}


class _Verifier:
    def verify(self, board, device_id):
        return Registration(True)


class _Keykeeper:
    def __init__(self, box="ab" * 145, chain=None):
        self.box, self._chain, self.asked = box, chain, []

    def lockbox(self, sealed, record, signature):
        self.asked.append((sealed, record, signature))
        return self.box

    def chain(self):
        return self._chain


def _app(tmp_path, *, keykeeper=None, sealed=True, registered=True, registrar=True):
    store = SqliteMetadataStore(str(tmp_path / "ota.db"))
    store.migrate()
    store.set_meta("capability_secret", "x")
    store.add_account(ACCT, "Example")
    store.add_token(hash_token("tok"), "owner", ["observe"], account_id=ACCT)
    enc = {"alg": "aes-256-cbc", "wraps": {}, "iv": "00" * 16, "size": 3, "sha256": "00" * 32}
    if sealed:
        enc["sealed"] = SEALED
    store.add_release(release_id="rel1", product_id=BID, product="P", version="2.0.0",
                      payload_version=0x02000000, min_platform_version=0, image_sha256="ab" * 32,
                      image_size=3, representations=[{"format": "full", "url": "x.img.gz.enc",
                                                      "size": 16, "enc": enc}],
                      manifest_key="m/rel1", image_key="i/rel1", account_id=ACCT)
    store.add_rollout(rollout_id="ro1", release_id="rel1", product_id=BID, cohort="__default__",
                      percent=100, account_id=ACCT)
    if registered:
        store.put_registration(DEV, ACCT, record=b'{"r":1}', signature="5e", identity="04",
                               exchange="04", desk_key_id=1, issued="2026-10-10T12:00:00Z",
                               registered_by="desk")
    storage = LocalArtifactStorage(str(tmp_path / "blobs"))
    storage.put("m/rel1", b"MANI", "application/octet-stream")
    settings = ServerSettings(base_url="https://ota.test", poll_jitter=0,
                              swd_ids_verify_url="u" if registrar else "",
                              swd_ids_verify_token="t")
    app = create_app(settings, storage=storage, metastore=store, verifier=_Verifier(),
                     keykeeper=keykeeper)
    return TestClient(app), store


def _sub(tmp_path, name):
    path = tmp_path / name
    path.mkdir()
    return path


def _check(c):
    return c.post("/api/v1/check", json={"device_id": "dev1", "product_id": BID,
                                         "payload_version": 0x01000000, "board": "OPENMV_N6",
                                         "account_id": ACCT}).json()


def test_the_offer_carries_the_camera_s_lockbox(tmp_path):
    kk = _Keykeeper()
    c, store = _app(tmp_path, keykeeper=kk)
    r = _check(c)
    assert r["update"] is True and r["lockbox"] == "ab" * 145
    assert kk.asked == [(SEALED, b'{"r":1}', "5e")]          # the registration, verbatim
    assert store.get_device(DEV)["last_offered_release_id"] == "rel1"


def test_no_lockbox_no_offer(tmp_path):
    for kw in ({"keykeeper": None}, {"keykeeper": _Keykeeper(box=None)},
               {"keykeeper": _Keykeeper(), "registered": False}):
        c, store = _app(_sub(tmp_path, str(len(list(tmp_path.iterdir())))), **kw)
        r = _check(c)
        assert r["update"] is False, kw
        assert "lockbox" not in r
        row = store.get_device(DEV)
        assert row is not None and row["last_offered_release_id"] is None
        assert store.get_rollout("ro1")["attempted"] == 0


def test_a_release_not_encrypted_for_each_camera_is_offered_as_before(tmp_path):
    kk = _Keykeeper()
    c, _ = _app(tmp_path, keykeeper=kk, sealed=False)
    r = _check(c)
    assert r["update"] is True and "lockbox" not in r and kk.asked == []


def test_read_only_serving_needs_a_lockbox_too(tmp_path):
    c, _ = _app(tmp_path, keykeeper=_Keykeeper(), registrar=False)
    assert _check(c)["lockbox"] == "ab" * 145
    c, _ = _app(_sub(tmp_path, "x"), keykeeper=None, registrar=False)
    assert _check(c)["update"] is False


def test_the_chain_is_passed_on_to_publishers(tmp_path):
    chain = [{"record": "{}", "signature": "00"}]
    c, _ = _app(tmp_path, keykeeper=_Keykeeper(chain=chain))
    assert c.get("/api/v1/keykeeper/chain").json() == {"chain": chain}
    c, _ = _app(_sub(tmp_path, "a"), keykeeper=_Keykeeper(chain=None))
    assert c.get("/api/v1/keykeeper/chain").status_code == 503
    c, _ = _app(_sub(tmp_path, "b"))
    assert c.get("/api/v1/keykeeper/chain").status_code == 404


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = json.dumps(body) if not isinstance(body, str) else body

    def json(self):
        if isinstance(self._body, str):
            raise ValueError
        return self._body


class _Http:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def _next(self, method, url, **kw):
        self.calls.append((method, url, kw.get("headers"), kw.get("json")))
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    def post(self, url, **kw):
        return self._next("POST", url, **kw)

    def get(self, url, **kw):
        return self._next("GET", url, **kw)


def test_the_client_asks_keykeeper_with_the_shared_secret():
    http = _Http(_Resp(200, {"lockbox": "cd"}), _Resp(422, {"detail": "no"}), OSError("down"),
                 _Resp(200, "not json"), _Resp(200, {"chain": [1]}), _Resp(500, {}),
                 OSError("down"), _Resp(200, {"chain": "x"}))
    kk = kk_mod.KeykeeperClient("http://kk:8090/", "s3cret", http)
    assert kk.lockbox({"v": 1}, b"rec", "sig") == "cd"
    method, url, headers, body = http.calls[0]
    assert (method, url) == ("POST", "http://kk:8090/v1/lockbox")
    assert headers == {"Authorization": "Bearer s3cret"}
    assert body == {"sealed": {"v": 1}, "record": "rec", "signature": "sig"}
    assert kk.lockbox({}, b"", "") is None                    # refused
    assert kk.lockbox({}, b"", "") is None                    # unreachable
    assert kk.lockbox({}, b"", "") is None                    # garbled
    assert kk.chain() == [1]
    assert kk.chain() is None and kk.chain() is None and kk.chain() is None


def test_build_keykeeper_follows_the_settings():
    assert kk_mod.build_keykeeper(ServerSettings()) is None
    kk = kk_mod.build_keykeeper(ServerSettings(keykeeper_url="http://kk:8090",
                                               keykeeper_token="t"))
    assert kk.url == "http://kk:8090" and kk.token == "t"
    assert kk_mod.sealed_of(None) is None
    assert kk_mod.sealed_of({"representations": ["junk", {"enc": None}]}) is None

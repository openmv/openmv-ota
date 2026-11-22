"""Signed check-ins on the server.

A registered camera signs its check-in with the identity key its registration names. Only a
signed check-in gets the full treatment; an unsigned one -- or one signed at a stale time -- gets
update offers and nothing else, and a bad signature gets nothing. The good ones are noted on the
camera's registration, the bad ones counted there and audited.
"""

from __future__ import annotations

import json
import time

import pytest

from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from openmv_ota.ota import checkin_sig
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


def test_a_replayed_signed_check_in_gets_what_a_stale_one_gets(tmp_path):
    """The answer to a signed check-in carries day-long grants to publish as that camera. A
    capture of one, sent again inside the five-minute window, used to be answered in full."""
    c, store = _app(tmp_path)
    key = _register(store)
    body = _body()
    now = int(time.time())
    first = camera.sign(key, body, ts=now)
    _check(c, body, first)
    assert store.get_device(DEV) is not None                            # taken in full
    replay = _check(c, body, first)                                     # the same request again
    assert "live" not in replay and "ingest" not in replay
    assert store.get_registration(DEV)["last_refusal"] == "stale"
    older = _check(c, body, camera.sign(key, body, ts=now - 5))         # one from before it
    assert "live" not in older and "ingest" not in older
    # one request has many spellings that verify; none of them is a new request
    ts_sig = first.split(";")
    for variant in (first.upper().replace("V=", "v=").replace("TS=", "ts=").replace("SIG=", "sig="),
                    "; ".join(ts_sig), ";".join(reversed(ts_sig)), first + ";x=1",
                    first.replace("ts=%d" % now, "ts=+%d" % now)):
        r = _check(c, body, variant)
        assert "live" not in r and "ingest" not in r, variant
    # the camera's next request is new
    store.note_signed_checkin(DEV)
    r = _check(c, body, camera.sign(key, body, ts=now + 1))
    assert "live" in r
    # ...and a signed check-in is not a signed feedback report, nor the other way round
    fb = json.dumps({"device_id": "dev1", "board": "OPENMV_N6", "product_id": BID,
                     "release_id": "rel1", "status": "installed"}).encode()
    as_checkin = camera.sign(key, fb, ts=now + 2)
    assert c.post("/api/v1/feedback", content=fb, headers={
        "Content-Type": "application/json", "X-OMV-Signature": as_checkin}).json() == {"ok": False}
    as_feedback = camera.sign(key, fb, ts=now + 3, context=checkin_sig.FEEDBACK_CONTEXT)
    assert _check(c, fb, as_feedback) == NOTHING


def test_a_camera_route_takes_no_more_than_a_check_in_is(tmp_path):
    """No credential on these routes, and the body is read before the rate limit is asked."""
    from openmv_ota.server import app as app_mod
    c, _store = _app(tmp_path)
    big = b'{"device_id": "' + b"a" * (app_mod._DEVICE_BODY_MAX + 1) + b'"}'
    for path in ("/api/v1/check", "/api/v1/feedback"):
        r = c.post(path, content=big, headers={"Content-Type": "application/json"})
        assert r.status_code == 413 and r.json() == {"detail": "request body too large"}

        def chunks():                                # no Content-Length to refuse up front
            for _ in range(app_mod._DEVICE_BODY_MAX // 4096 + 2):
                yield b" " * 4096
        assert c.post(path, content=chunks(),
                      headers={"Content-Type": "application/json"}).status_code == 413
    ok = c.post("/api/v1/check", content=_body(), headers={"Content-Type": "application/json"})
    assert ok.status_code == 200


def test_a_camera_has_one_name_here_however_the_registrar_would_read_it(tmp_path):
    """The registrar answers for a board name in any case and with space around it, and for
    an id with space around it. Each spelling was its own identity here: its own row, free
    to claim a different account and be offered that account's release."""
    c, store = _app(tmp_path)
    for board, dev, acct in (("openmv_n6", "dev1", ACCT), ("OPENMV_N6 ", "dev1", ACCT),
                             (" OPENMV_N6", "dev1", ACCT), ("OPENMV_N6", " dev1", ACCT),
                             ("OPENMV_N6", "dev1\n", ACCT), ("OPENMV_N6", "dev/1", ACCT),
                             ("OPENMV_N6", "dev1", "x" * 65), ("OPENMV_N6", "dev1", "a b"),
                             ("OPENMV_N6", "", ACCT)):
        body = json.dumps({"device_id": dev, "board": board, "product_id": BID,
                           "payload_version": 0x01000000, "account_id": acct}).encode()
        assert _check(c, body) == NOTHING, (board, dev, acct)
        report = json.dumps({"device_id": dev, "board": board, "product_id": BID,
                             "release_id": "rel1", "status": "failed", "account_id": acct})
        assert c.post("/api/v1/feedback", content=report,
                      headers={"Content-Type": "application/json"}).json() == {"ok": False}
    assert store.query_all("SELECT * FROM device_accounts") == []
    assert _check(c, _body())["update"] is True               # the camera's own name still works


def test_no_route_reads_a_body_larger_than_it_could_use_before_it_knows_the_caller(tmp_path):
    from openmv_ota.server import app as app_mod
    c, _store = _app(tmp_path)
    big = b"{" + b" " * app_mod._ADMIN_BODY_MAX + b"}"
    for method, path in (("POST", "/api/v1/admin/webhooks"), ("POST", "/api/v1/admin/rollouts"),
                         ("POST", "/api/v1/admin/registrations"), ("PUT", "/api/v1/admin/roots"),
                         ("PATCH", "/api/v1/admin/devices/x/pin")):
        r = c.request(method, path, content=big, headers={"Content-Type": "application/json"})
        assert r.status_code == 413, (method, path, r.status_code)
    # a release upload is large by nature: the bearer token is asked for before a byte is read
    read = []

    def parts():
        read.append(1)
        yield b"x" * 4096
    r = c.post("/api/v1/admin/releases", content=parts(),
               headers={"Content-Type": "multipart/form-data; boundary=b"})
    assert r.status_code == 401 and read == []
    r = c.post("/api/v1/admin/releases", content=parts(),
               headers={"Content-Type": "multipart/form-data; boundary=b",
                        "Authorization": "Bearer nope"})
    assert r.status_code == 401 and read == []
    assert c.get("/healthz").status_code == 200


def test_a_release_upload_is_refused_by_scope_and_by_size_before_it_is_read(tmp_path):
    """The framework spools a multipart body before the route's scope check runs: any valid
    token, an observe-only one included, could have the server write as much as it sent."""
    c, store = _app(tmp_path)
    store.add_token(hash_token("ro"), "viewer", ["observe"], account_id=ACCT)
    store.add_token(hash_token("pub"), "ci", ["publish"], account_id=ACCT)
    read = []

    def parts():
        read.append(1)
        yield b"x" * 4096
    ct = {"Content-Type": "multipart/form-data; boundary=b"}
    r = c.post("/api/v1/admin/releases", content=parts(), headers=dict(ct, Authorization="Bearer ro"))
    assert r.status_code == 403 and r.json() == {"detail": "missing scope: publish"} and read == []
    st = c.app.state.settings
    cap = st.max_manifest_bytes + st.max_sbom_bytes + 2 * st.max_image_bytes + 1024 * 1024
    r = c.post("/api/v1/admin/releases", content=parts(),
               headers=dict(ct, Authorization="Bearer pub", **{"Content-Length": str(cap + 1)}))
    assert r.status_code == 413 and read == []
    r = c.post("/api/v1/admin/releases", content=parts(), headers=dict(ct, Authorization="Bearer pub"))
    assert r.status_code in (400, 422) and read == [1]          # a publisher's body is read


def test_the_keykeeper_chain_is_rate_limited_like_the_other_open_routes(tmp_path):
    c, _store = _app(tmp_path)

    class _KK:
        def chain(self):
            return [{"record": "{}", "signature": "00"}]
    c.app.state.keykeeper = _KK()
    assert c.get("/api/v1/keykeeper/chain").status_code == 200
    c.app.state.ratelimit.allow = lambda ip: False
    assert c.get("/api/v1/keykeeper/chain").status_code == 429


def test_the_replay_cache_is_bounded_and_the_body_limit_does_not_hide_an_apps_own_error(monkeypatch):
    import asyncio

    from openmv_ota.server import app as app_mod
    monkeypatch.setattr(app_mod, "_LAST_SIGNED", {})
    monkeypatch.setattr(app_mod, "_LAST_SIGNED_MAX", 2)
    assert app_mod._fresh("a", 1) and app_mod._fresh("b", 1)
    assert app_mod._fresh("c", 1) and set(app_mod._LAST_SIGNED) == {"c"}      # cleared, then kept
    assert not app_mod._fresh("c", 1) and app_mod._fresh("c", 2)

    async def broken(scope, receive, send):
        await receive()
        raise RuntimeError("the app's own fault")

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}
    sent = []

    async def send(message):
        sent.append(message)
    scope = {"type": "http", "method": "POST", "path": "/api/v1/check", "headers": []}
    with pytest.raises(RuntimeError, match="own fault"):
        asyncio.run(app_mod._BodyLimit(broken)(scope, receive, send))
    assert sent == []

    async def big():
        return {"type": "http.request", "body": b"x" * (app_mod._DEVICE_BODY_MAX + 1), "more_body": False}
    asyncio.run(app_mod._BodyLimit(broken)(scope, big, send))    # over the cap: the 413, not the error
    assert sent[0]["status"] == 413


def test_a_forgotten_camera_stays_with_the_account_it_is_registered_to(tmp_path):
    """Forgetting a camera drops its fleet row and binding and keeps its registration. The
    next check-in for that id, signed or not, used to be believed about its account: another
    tenant naming the id took the binding, and the camera's own signed check-ins followed."""
    c, store = _app(tmp_path)
    key = _register(store)
    body = _body()
    _check(c, body, camera.sign(key, body))
    assert store.get_device(DEV)["account_id"] == ACCT
    store.forget_device(DEV)
    store.add_account("acct_other", "Other")
    theirs = json.dumps(dict(json.loads(body), account_id="acct_other")).encode()
    _check(c, theirs)                                                   # unsigned, by anyone
    assert store.device_account(DEV)["account_id"] == ACCT
    _check(c, body, camera.sign(key, body))
    assert store.get_device(DEV)["account_id"] == ACCT

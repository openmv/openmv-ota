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
    # A DIFFERENT REASON IS NOT NEWS. A refusal needs no key, so two reasons in turn (a header
    # that doesn't parse, a version nobody speaks) made every request a row in the account's
    # audit trail and a delivery to each of its webhooks.
    _check(c, body, camera.sign(key, body, ts=int(time.time()) - 3600))
    for header in ("v=2;ts=1;sig=00", "v=1;ts=1;sig=zz") * 3:
        assert _check(c, body, header) == NOTHING
    reg = store.get_registration(DEV)
    assert (reg["refused_checkins"], reg["last_refusal"]) == (9, "malformed")   # still counted
    assert len(_audit(store, "device.checkin_refused")) == 1
    # A day on, a camera that is still failing is news again.
    store.execute("UPDATE device_registrations SET last_refused_at = ? WHERE device_id = ?",
                  ("2020-01-01T00:00:00+00:00", DEV))
    _check(c, body, forged)
    assert len(_audit(store, "device.checkin_refused")) == 2


def test_a_refused_check_in_is_filed_under_the_cameras_product_not_the_one_it_names(tmp_path):
    c, store = _app(tmp_path)
    key = _register(store)
    body = _body()
    _check(c, body, camera.sign(key, body))                            # in the fleet under BID
    named = json.loads(body)
    named["product_id"] = BID + 1
    _check(c, json.dumps(named).encode(), "v=1;ts=1;sig=zz")
    assert [e["product_id"] for e in _audit(store, "device.checkin_refused")] == [BID]


def test_a_camera_that_changes_product_by_itself_is_served_and_audited(tmp_path):
    c, store = _app(tmp_path)
    key = _register(store)
    ts = int(time.time()) - 100

    def report(**kw):
        nonlocal ts
        ts += 1
        body = json.dumps({**json.loads(_body()), **kw}).encode()
        return _check(c, body, camera.sign(key, body, ts=ts))

    report()
    assert "live" in report(product_id=BID + 1)                        # served like any other
    assert store.get_device(DEV)["product_id"] == BID + 1              # filed under what it says
    assert store.get_registration(DEV)["product_id"] == BID + 1
    seen = _audit(store, "device.product_mismatch")
    assert [(e["product_id"], e["data"]) for e in seen] == \
        [(BID, {"product_id": BID, "reported": BID + 1})]              # under the one it left
    report(product_id=BID + 1)                                         # there now: not news
    assert len(_audit(store, "device.product_mismatch")) == 1
    aday = "UPDATE device_registrations SET product_mismatch_at = '2020-01-01T00:00:00+00:00'"
    store.execute(aday)                                                # a day on
    # SENT there, it is the account's doing and no event. Pinned to a release of the other
    # product, the camera is offered it; the offer on its row says so after the pin is lifted.
    _rel2(store)
    report(product_id=BID + 2)                                         # somewhere else by itself
    assert len(_audit(store, "device.product_mismatch")) == 2
    store.set_device_pin(DEV, "rel2", account_id=ACCT)
    assert report(product_id=BID + 2)["release_id"] == "rel2"
    store.set_device_pin(DEV, None, account_id=ACCT)
    assert "live" in report(product_id=BID + 1, payload_version=0x03000000)
    assert store.get_device(DEV)["product_id"] == BID + 1
    assert len(_audit(store, "device.product_mismatch")) == 2
    # A pin alone says so too (a camera offered while it could not sign leaves no offer on its row).
    store.execute(aday)
    report(product_id=BID + 2, payload_version=0x03000000)             # away again by itself
    store.set_device_pin(DEV, "rel2", account_id=ACCT)
    store.execute("UPDATE devices SET last_offered_release_id = NULL WHERE device_id = ?", (DEV,))
    report(product_id=BID + 1, payload_version=0x03000000)
    assert len(_audit(store, "device.product_mismatch")) == 3          # only the three it made alone


def test_a_product_change_is_audited_once_a_day_and_not_from_the_cameras_budget(tmp_path):
    """The camera's audit budget is spent by what the camera chooses to report. Drawn from
    it, the one event that says a camera changed product was the camera's to switch off."""
    from openmv_ota.server.metastore import DEVICE_AUDIT_PER_DAY
    c, store = _app(tmp_path)
    key = _register(store)
    ts = int(time.time()) - 200

    def report(**kw):
        nonlocal ts
        ts += 1
        body = json.dumps({**json.loads(_body()), **kw}).encode()
        return _check(c, body, camera.sign(key, body, ts=ts))

    for i in range(DEVICE_AUDIT_PER_DAY + 2):
        report(fallback_reason="r%d" % i)                              # the budget, spent
    assert len(_audit(store, "device.fallback")) == DEVICE_AUDIT_PER_DAY
    assert "live" in report(product_id=BID + 1)
    assert len(_audit(store, "device.product_mismatch")) == 1          # audited all the same
    for i in range(6):
        report(product_id=BID + 2 + i % 2)                             # hopping: a row a day
    assert len(_audit(store, "device.product_mismatch")) == 1


def test_a_forgotten_camera_that_comes_back_as_another_product_is_audited(tmp_path):
    """A token limited to one product forgets the fleet row and no more: the registration
    still says what the camera was, so coming back under another product is a change. A
    token for the whole account clears that too (the admin route does), and so does
    registering the camera again at a desk: it starts under what it reports, with no event."""
    c, store = _app(tmp_path)
    key = _register(store)
    body = _body()
    other = json.dumps({**json.loads(body), "product_id": BID + 1}).encode()
    _check(c, body, camera.sign(key, body, ts=int(time.time()) - 9))
    store.forget_device(DEV)                                           # as a limited token does
    assert "live" in _check(c, other, camera.sign(key, other, ts=int(time.time()) - 8))
    assert [e["data"] for e in _audit(store, "device.product_mismatch")] == \
        [{"product_id": BID, "reported": BID + 1}]
    store.forget_device(DEV)
    store.set_registration_product(DEV, None)                          # the whole account's
    _check(c, body, camera.sign(key, body, ts=int(time.time()) - 7))
    assert store.get_registration(DEV)["product_id"] == BID
    _register(store)                                                   # registered again
    assert store.get_registration(DEV)["product_id"] is None
    assert "live" in _check(c, other, camera.sign(key, other, ts=int(time.time()) - 6))
    assert store.get_device(DEV)["product_id"] == BID + 1
    assert len(_audit(store, "device.product_mismatch")) == 1
    assert store.registration_product("OPENMV_N6:nobody") is None


def test_a_cameras_words_are_cut_short_not_refused(tmp_path):
    c, store = _app(tmp_path)
    key = _register(store)
    body = json.dumps({**json.loads(_body()), "app_version": "v" * 5000,
                       "fallback_reason": "r" * 5000, "streams": ["s" * 500] * 100}).encode()
    assert _check(c, body, camera.sign(key, body))["update"] is True
    row = store.get_device(DEV)
    assert len(row["current_version"]) == 200 and len(row["fallback_reason"]) == 200
    assert len(str(row["streams"])) <= 16 * (64 + 4)                   # 16 names of 64 at most


def test_one_camera_is_one_of_each_rollout_counter(tmp_path):
    c, store = _app(tmp_path)
    key = _register(store)
    store.update_rollout("ro1", failure_threshold=0.9)
    ts = int(time.time()) - 60

    def report(**kw):
        nonlocal ts
        ts += 1
        body = json.dumps({**json.loads(_body()), **kw}).encode()
        return _check(c, body, camera.sign(key, body, ts=ts))

    report()                                                           # offered: attempted
    for _ in range(4):                                                 # a reason, none, a reason...
        report(fallback_reason="x")
        report()
    ro = store.get_rollout("ro1")
    assert (ro["attempted"], ro["failures"], ro["state"]) == (1, 1, "paused")
    # ...and running the release then the old one again does not enter the rollout twice.
    store.update_rollout("ro1", state="active")
    report(payload_version=0x02000000)
    report()
    report(payload_version=0x02000000)
    ro = store.get_rollout("ro1")
    assert (ro["attempted"], ro["updated"], ro["failures"]) == (1, 1, 1)
    with pytest.raises(ValueError):
        store.count_once("ro1", DEV, "percent")


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
        [{"reason": "bad signature"}]


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


def test_what_a_camera_reports_reaches_the_audit_trail_on_a_daily_budget(tmp_path):
    """Eight made-up journal entries a check-in, a fallback reason that changes every time,
    a failed install as often as it likes: each was an audit row and a webhook delivery, for
    as long as a camera with a registered key cared to send them."""
    from openmv_ota.server.metastore import DEVICE_AUDIT_PER_DAY
    c, store = _app(tmp_path)
    key = _register(store)
    ts = int(time.time()) - 200
    seq = 0

    def report(**kw):
        nonlocal ts, seq
        ts += 1
        journal = [[seq + i, 0, 1, 0, 0, 0] for i in range(8)]
        seq += 8
        body = json.dumps({**json.loads(_body()), "stub": {"version": 1, "journal": journal},
                           **kw}).encode()
        return _check(c, body, camera.sign(key, body, ts=ts))

    for i in range(12):
        report(fallback_reason="r%d" % i)
    caused = [e for e in store.read_audit(account_id=ACCT)
              if e["action"] in ("device.boot", "device.fallback")]
    assert len(caused) == DEVICE_AUDIT_PER_DAY                         # 108 asked for
    for _ in range(3):                                                 # failed installs too
        fb = json.dumps({"device_id": "dev1", "board": "OPENMV_N6", "product_id": BID,
                         "release_id": "rel1", "status": "failed", "reason": "x"}).encode()
        ts += 1
        c.post("/api/v1/feedback", content=fb, headers={
            "Content-Type": "application/json",
            "X-OMV-Signature": camera.sign(key, fb, ts=ts, context=checkin_sig.FEEDBACK_CONTEXT)})
    assert _audit(store, "install.failed") == []
    assert store.deployment_counts("rel1")["failed"] == 1              # recorded all the same
    # The next day it has a day's worth again; a camera with no fleet row has none.
    store.execute("UPDATE devices SET audit_day = ? WHERE device_id = ?", ("2020-01-01", DEV))
    assert store.audit_budget(DEV) is True
    assert store.audit_budget("OPENMV_N6:nobody") is False


def test_a_check_ins_numbers_fit_what_stores_them(tmp_path):
    """A version past its column was an error from the store, after the request had written
    its audit rows: the journal mark never moved, and the same rows were written again."""
    c, store = _app(tmp_path)
    key = _register(store)
    for field, value in (("payload_version", 2 ** 32), ("payload_version", -1),
                         ("publish_seq", 2 ** 48), ("product_id", 2 ** 63)):
        body = json.dumps({**json.loads(_body()), field: value}).encode()
        assert camera.post(c, body, camera.sign(key, body)).status_code == 422
    body = json.dumps({**json.loads(_body()), "stub": {"version": 2 ** 40, "reason": 0},
                       "slots": [{"running": True, "body_sha256": "ab" * 5000},
                                 {"running": False, "payload_version": "junk"}]}).encode()
    assert _check(c, body, camera.sign(key, body))["update"] is True
    row = store.get_device(DEV)
    assert row["stub_version"] is None and row["fallback_payload_version"] is None
    assert len(row["body_sha256"]) == 64
    assert store.read_audit(account_id=ACCT)[0]["action"]              # and it enrolled once
    assert len(_audit(store, "device.enrolled")) == 1


def test_a_derived_product_id_checks_in(tmp_path):
    """A product id is 63 bits: one a project derives is the top of a SHA-256, and only a
    hand-set small one fits 32. Bounded to 32 at check-in, every camera of a project on
    derived ids was answered 422 -- no offer, no grant, no fleet row."""
    from openmv_ota.project.config import derive_product_id
    c, store = _app(tmp_path)
    key = _register(store)
    derived = derive_product_id("A Product", "OPENMV_N6")
    assert derived > 0xFFFFFFFF
    for pid in (derived, (1 << 63) - 1):
        body = json.dumps({**json.loads(_body()), "product_id": pid}).encode()
        r = camera.post(c, body)                                       # unsigned: offers only
        assert r.status_code == 200 and r.json() == NOTHING
    body = json.dumps({**json.loads(_body()), "product_id": derived}).encode()
    assert camera.post(c, body, camera.sign(key, body)).status_code == 200
    assert store.get_device(DEV)["product_id"] == derived


def test_a_camera_with_a_row_and_no_noted_product_gets_it_noted(tmp_path):
    """Registered after it had a fleet row, or enrolled while a forget was clearing the note:
    a row, and nothing on the registration to hold it to once the row was forgotten."""
    c, store = _app(tmp_path)
    key = _register(store)
    body = _body()
    _check(c, body, camera.sign(key, body))
    store.set_registration_product(DEV, None)
    assert store.get_registration(DEV)["product_id"] is None
    _check(c, body, camera.sign(key, body))                            # the same product again
    assert store.get_registration(DEV)["product_id"] == BID
    odd = json.dumps({**json.loads(body), "app_version": "v\u0000\ud800x"}).encode()
    assert _check(c, odd, camera.sign(key, odd))["update"] is True     # stored without them
    assert "\x00" not in store.get_device(DEV)["current_version"]


def _rel2(store):
    store.add_release(release_id="rel2", product_id=BID + 1, product="Q", version="3.0.0",
                      payload_version=0x03000000, min_platform_version=0, image_sha256="ab" * 32,
                      image_size=3, representations=[{"format": "full", "url": "x.img.gz",
                                                      "size": 3}],
                      manifest_key="m/rel1", image_key="i/rel1", account_id=ACCT)


def test_a_cohort_pinned_across_products_moves_its_cameras(tmp_path):
    c, store = _app(tmp_path)
    key = _register(store)
    _rel2(store)
    body = _body()
    _check(c, body, camera.sign(key, body))
    store.execute("UPDATE devices SET last_offered_release_id = NULL WHERE device_id = ?", (DEV,))
    store.set_cohort_pin(BID, "__default__", "rel2", account_id=ACCT)
    moved = json.dumps({**json.loads(body), "product_id": BID + 1,
                        "payload_version": 0x03000000}).encode()
    assert "live" in _check(c, moved, camera.sign(key, moved))
    assert store.get_device(DEV)["product_id"] == BID + 1
    assert _audit(store, "device.product_mismatch") == []              # sent, not hopped


def test_a_version_with_a_major_past_127_checks_in(tmp_path):
    """A payload version is major << 24 with a major up to 255: 32 bits, not 31."""
    c, store = _app(tmp_path)
    key = _register(store)
    body = json.dumps({**json.loads(_body()), "payload_version": 0xC8000000,
                       "slots": [{"running": True, "payload_version": 0xC8000000},
                                 {"running": False, "payload_version": 0xC7000000}]}).encode()
    assert camera.post(c, body, camera.sign(key, body)).status_code == 200
    row = store.get_device(DEV)
    assert (row["current_payload_version"], row["fallback_payload_version"]) == \
        (0xC8000000, 0xC7000000)


def test_a_camera_cannot_spend_the_days_event_and_settle_somewhere_else_unseen(tmp_path):
    """The event is once a day. The product a camera is known under moves when the account
    sent it or the move was audited, so one made while the day's event is spent is still a
    change the next day: the trail ends up saying where the camera is."""
    c, store = _app(tmp_path)
    key = _register(store)
    ts = int(time.time()) - 100

    def report(**kw):
        nonlocal ts
        ts += 1
        body = json.dumps({**json.loads(_body()), **kw}).encode()
        return _check(c, body, camera.sign(key, body, ts=ts))

    report()
    report(product_id=BID + 5)                                         # the day's event
    report()                                                           # back, as if a blip
    report(product_id=BID + 9)                                         # and somewhere else
    assert [e["data"]["reported"] for e in _audit(store, "device.product_mismatch")] == [BID + 5]
    assert store.get_device(DEV)["product_id"] == BID + 9              # served there all the same
    assert store.registration_product(DEV) == BID + 5                  # known as last audited
    store.execute("UPDATE device_registrations SET product_mismatch_at = "
                  "'2020-01-01T00:00:00+00:00'")                       # a day on
    report(product_id=BID + 9)
    assert [e["data"] for e in _audit(store, "device.product_mismatch")][-1] == \
        {"product_id": BID + 5, "reported": BID + 9}
    assert store.registration_product(DEV) == BID + 9
    report(product_id=BID + 9)
    assert len(_audit(store, "device.product_mismatch")) == 2


def test_an_offer_made_after_a_camera_moved_itself_does_not_vouch_for_the_move(tmp_path):
    """A rollout offers the release of the product a camera reports. The row kept that offer
    until the next one, and "the release it was last offered is one of that product" then
    read every later move there as the account's own."""
    c, store = _app(tmp_path)
    key = _register(store)
    _rel2(store)
    store.add_rollout(rollout_id="ro2", release_id="rel2", product_id=BID + 1,
                      cohort="__default__", percent=100, state="active", account_id=ACCT)
    ts = int(time.time()) - 100

    def report(**kw):
        nonlocal ts
        ts += 1
        body = json.dumps({**json.loads(_body()), **kw}).encode()
        return _check(c, body, camera.sign(key, body, ts=ts))

    aday = "UPDATE device_registrations SET product_mismatch_at = '2020-01-01T00:00:00+00:00'"
    report()
    assert report(product_id=BID + 1)["release_id"] == "rel2"          # offered where it went
    store.execute(aday)
    report()                                                           # back: nothing offered
    assert store.get_device(DEV)["last_offered_release_id"] != "rel2"
    store.execute(aday)
    report(product_id=BID + 1)
    assert len(_audit(store, "device.product_mismatch")) == 3          # each move, by itself


def test_two_check_ins_at_once_take_one_product_change_event_between_them(tmp_path):
    _c, store = _app(tmp_path)
    _register(store)
    assert store.note_product_mismatch(DEV) is True
    assert store.note_product_mismatch(DEV) is False
    assert store.note_product_mismatch("OPENMV_N6:nobody") is False

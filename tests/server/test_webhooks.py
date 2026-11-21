"""Webhooks: the audit log's push side. Endpoints subscribe, the store fans matching
entries out as deliveries, the deliverer POSTs them signed and retries on a fixed
backoff, and an endpoint that keeps failing is switched off. Against a fake receiver."""
from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from openmv_ota.server import webhooks as wh
from openmv_ota.server.app import create_app
from openmv_ota.server.auth import hash_token
from openmv_ota.server.metastore import SqliteMetadataStore
from openmv_ota.server.settings import ServerSettings
from openmv_ota.server.storage import LocalArtifactStorage
from openmv_ota.server.verify import Registration
from tests.server import camera


class _Verifier:
    def verify(self, *a, **k):
        return Registration(True)


class _Receiver:
    """The customer's endpoint: records every POST; answers what it is told to."""
    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.status = 200
        self.raise_error = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.raise_error:
            raise httpx.ConnectError("refused")
        return httpx.Response(self.status, text="ok")


def _resolve_public(host, port):
    return [(None, None, None, None, ("93.184.216.34", port))]


def _app(tmp_path, monkeypatch, **settings):
    """An app whose deliverer speaks to the fake receiver and whose URL check resolves
    every host to a public address (no DNS in tests)."""
    monkeypatch.setattr(wh.socket, "getaddrinfo", _resolve_public)
    store = SqliteMetadataStore(str(tmp_path / "ota.db"))
    store.migrate()
    store.set_meta("capability_secret", "x")
    store.add_token(hash_token("acct"), "ci", ["manage", "observe"], account_id="a1")
    store.add_token(hash_token("other"), "other", ["manage", "observe"], account_id="a2")
    cfg = ServerSettings(base_url="https://ota.test", swd_ids_verify_url="u", swd_ids_verify_token="t",
                         **settings)
    rx = _Receiver()
    deliverer = wh.Deliverer(store, cfg, http=httpx.Client(transport=httpx.MockTransport(rx)))
    app = create_app(cfg, metastore=store, storage=LocalArtifactStorage(str(tmp_path / "blobs")),
                     verifier=_Verifier(), webhooks=deliverer)
    return TestClient(app), store, rx, deliverer


AUTH = {"Authorization": "Bearer acct"}
OTHER = {"Authorization": "Bearer other"}


def _verify(secret: str, request: httpx.Request) -> dict:
    """What a receiver does: check the signature, then trust the body."""
    sig = dict(part.split("=", 1) for part in request.headers["x-openmv-signature"].split(","))
    body = request.content
    expect = hmac.new(secret.encode(), sig["t"].encode() + b"." + body, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(expect, sig["v1"])
    return json.loads(body)


def test_an_endpoint_receives_matching_events_signed(tmp_path, monkeypatch):
    c, store, rx, deliverer = _app(tmp_path, monkeypatch)
    made = c.post("/api/v1/admin/webhooks", headers=AUTH,
                  json={"url": "https://hooks.example/ota", "events": ["rollout.*", "device.forget"],
                        "description": "ops"}).json()
    secret = made["secret"]
    assert made["webhook_id"].startswith("wh_") and secret.startswith("whsec_") and made["active"] == 1
    # the listing never shows the secret, and carries the catalogue
    listed = c.get("/api/v1/admin/webhooks", headers=AUTH).json()
    assert [h["webhook_id"] for h in listed["webhooks"]] == [made["webhook_id"]]
    assert "secret" not in listed["webhooks"][0] and "rollout.autopause" in listed["events"]
    # creating the endpoint was itself an event, but not one this subscription names
    store.append_audit(actor="ci", action="rollout.stop", entity_type="rollout", entity_id="ro_1",
                       data={"why": "done"}, account_id="a1")
    store.append_audit(actor="ci", action="cohort.create", entity_type="cohort", entity_id="beta",
                       account_id="a1")                                     # not subscribed
    store.append_audit(actor="ci", action="rollout.stop", entity_type="rollout", entity_id="ro_9",
                       account_id="a2")                                     # another account
    assert deliverer.run_once(now=time.time() + 1) == {"sent": 1, "failed": 0, "dead": 0}
    assert len(rx.calls) == 1
    req = rx.calls[0]
    # sent to the ADDRESS the name was checked at, asking for the name (Host, and the TLS server name)
    assert str(req.url) == "https://93.184.216.34/ota" and req.headers["host"] == "hooks.example"
    assert req.extensions["sni_hostname"] == "hooks.example"
    assert req.headers["content-type"] == "application/json"
    assert req.headers["x-openmv-event"] == "rollout.stop" and req.headers["x-openmv-attempt"] == "1"
    body = _verify(secret, req)
    assert body["type"] == "rollout.stop" and body["account_id"] == "a1" and body["actor"] == "ci"
    assert body["entity"] == {"type": "rollout", "id": "ro_1"} and body["data"] == {"why": "done"}
    assert body["id"] == f"evt_{body['seq']}" and body["delivery"]["attempt"] == 1
    # the delivery record says it landed
    dl = c.get(f"/api/v1/admin/webhooks/{made['webhook_id']}/deliveries", headers=AUTH).json()
    assert dl["total"] == 1 and dl["deliveries"][0]["status"] == "delivered"
    assert dl["deliveries"][0]["last_code"] == 200 and dl["deliveries"][0]["event"] == "rollout.stop"
    shown = c.get(f"/api/v1/admin/webhooks/{made['webhook_id']}", headers=AUTH).json()
    assert shown["last_status"] == 200 and shown["failures"] == 0 and "secret" not in shown
    # a tampered signature does not verify
    with pytest.raises(AssertionError):
        _verify("whsec_wrong", req)


def test_retries_back_off_then_die_and_a_dead_endpoint_is_disabled(tmp_path, monkeypatch):
    c, store, rx, deliverer = _app(tmp_path, monkeypatch)
    hook = c.post("/api/v1/admin/webhooks", headers=AUTH,
                  json={"url": "https://hooks.example/x", "events": ["release.*", "cohort.*"]}).json()
    wid = hook["webhook_id"]
    rx.status = 503
    store.append_audit(actor="ci", action="release.publish", entity_type="release", entity_id="rel_1",
                       account_id="a1")
    t = time.time() + 1
    # attempt 1 fails: pending again, due after the first backoff
    assert deliverer.run_once(now=t)["failed"] == 1
    d = c.get(f"/api/v1/admin/webhooks/{wid}/deliveries", headers=AUTH).json()["deliveries"]
    d = [x for x in d if x["event"] == "release.publish"][0]
    assert d["status"] == "pending" and d["attempt"] == 1 and d["last_code"] == 503
    assert d["next_at"] == t + wh.BACKOFF_S[0]
    assert deliverer.run_once(now=t + 1)["sent"] == 0 and len(rx.calls) == 1   # not due yet
    # walk the whole schedule
    now = t
    for wait in wh.BACKOFF_S:
        now += wait
        out = deliverer.run_once(now=now)
        assert out["failed"] + out["dead"] >= 1
    d = [x for x in c.get(f"/api/v1/admin/webhooks/{wid}/deliveries", headers=AUTH).json()["deliveries"]
         if x["event"] == "release.publish"][0]
    assert d["status"] == "dead" and d["attempt"] == wh.MAX_ATTEMPTS
    # dead: retry by hand, and it goes out when the receiver is back
    assert c.post(f"/api/v1/admin/webhooks/{wid}/deliveries/{d['delivery_id']}/retry",
                  headers=AUTH).json()["status"] == "pending"
    rx.status = 200
    assert deliverer.run_once(now=now + 1)["sent"] >= 1
    d = c.get(f"/api/v1/admin/webhooks/{wid}/deliveries", headers=AUTH, params={"status": "delivered"}).json()
    assert any(x["event"] == "release.publish" for x in d["deliveries"])
    assert c.post(f"/api/v1/admin/webhooks/{wid}/deliveries/{d['deliveries'][0]['delivery_id']}/retry",
                  headers=AUTH).status_code == 409                          # not dead
    # a receiver that is down for good: after DISABLE_AFTER failed attempts in a row the
    # endpoint is switched off, pending deliveries die, and the audit says so
    rx.raise_error = True
    for i in range(wh.DISABLE_AFTER):
        store.append_audit(actor="ci", action="cohort.create", entity_type="cohort",
                           entity_id=f"c{i}", account_id="a1")
    now += 10
    out = deliverer.run_once(now=now)
    assert out["failed"] + out["dead"] == wh.DISABLE_AFTER
    shown = c.get(f"/api/v1/admin/webhooks/{wid}", headers=AUTH).json()
    assert shown["active"] == 0 and "consecutive" in shown["disabled_reason"]
    assert store.query_one("SELECT COUNT(*) AS n FROM webhook_deliveries WHERE status = 'pending'")["n"] == 0
    assert [e["action"] for e in c.get("/api/v1/admin/audit", headers=AUTH, params={"action": "webhook.disabled"}).json()["events"]]
    # re-enabling clears the count; a delivery queued for a disabled endpoint is dead on arrival
    rx.raise_error = False
    assert c.patch(f"/api/v1/admin/webhooks/{wid}", headers=AUTH, json={"active": True}).json()["failures"] == 0
    c.patch(f"/api/v1/admin/webhooks/{wid}", headers=AUTH, json={"active": False})
    store.append_audit(actor="ci", action="cohort.create", entity_type="cohort", entity_id="late", account_id="a1")
    assert store.query_one("SELECT COUNT(*) AS n FROM webhook_deliveries WHERE status = 'pending'")["n"] == 0


def test_the_url_is_checked_and_everything_is_scoped_to_the_account(tmp_path, monkeypatch):
    c, store, rx, deliverer = _app(tmp_path, monkeypatch)
    for bad, why in (("ftp://x.example/", "https"), ("http://x.example/", "https"),
                     ("https://user:pw@x.example/", "credentials"), ("not a url", "https")):
        r = c.post("/api/v1/admin/webhooks", headers=AUTH, json={"url": bad})
        assert r.status_code == 400 and why in r.json()["detail"], bad
    monkeypatch.setattr(wh.socket, "getaddrinfo", lambda h, p: [(0, 0, 0, "", ("10.0.0.5", p))])
    r = c.post("/api/v1/admin/webhooks", headers=AUTH, json={"url": "https://intranet.example/"})
    assert r.status_code == 400 and "private" in r.json()["detail"]
    def no_dns(h, p):
        raise OSError("nope")
    monkeypatch.setattr(wh.socket, "getaddrinfo", no_dns)
    assert "resolve" in c.post("/api/v1/admin/webhooks", headers=AUTH,
                               json={"url": "https://nowhere.example/"}).json()["detail"]
    monkeypatch.setattr(wh.socket, "getaddrinfo", _resolve_public)
    # bad subscriptions
    for events in ([], ["rollout stop"], [""], ["x" * 65]):
        assert c.post("/api/v1/admin/webhooks", headers=AUTH,
                      json={"url": "https://ok.example/", "events": events}).status_code == 400
    hook = c.post("/api/v1/admin/webhooks", headers=AUTH, json={"url": "https://ok.example/"}).json()
    wid = hook["webhook_id"]
    # another account sees nothing of it
    assert c.get("/api/v1/admin/webhooks", headers=OTHER).json()["webhooks"] == []
    for method, path in (("get", ""), ("patch", ""), ("delete", ""), ("post", "/rotate"), ("post", "/test"),
                         ("get", "/deliveries")):
        r = getattr(c, method)(f"/api/v1/admin/webhooks/{wid}{path}", headers=OTHER,
                               **({"json": {"active": False}} if method == "patch" else {}))
        assert r.status_code == 404, (method, path)
    # update: nothing to change is a 400; each field lands; a bad status filter is a 400
    assert c.patch(f"/api/v1/admin/webhooks/{wid}", headers=AUTH, json={}).status_code == 400
    up = c.patch(f"/api/v1/admin/webhooks/{wid}", headers=AUTH,
                 json={"url": "https://ok.example/v2", "events": ["device.*"], "description": "d"}).json()
    assert (up["url"], up["events"], up["description"]) == ("https://ok.example/v2", ["device.*"], "d")
    assert c.get(f"/api/v1/admin/webhooks/{wid}/deliveries", headers=AUTH,
                 params={"status": "lost"}).status_code == 400
    # rotate: a new secret verifies, the old one does not
    old = hook["secret"]
    new = c.post(f"/api/v1/admin/webhooks/{wid}/rotate", headers=AUTH).json()["secret"]
    assert new != old and new.startswith("whsec_")
    ping = c.post(f"/api/v1/admin/webhooks/{wid}/test", headers=AUTH).json()
    assert ping["delivery_id"].startswith("dl_")
    deliverer.run_once(now=time.time() + 1)
    req = rx.calls[-1]
    assert req.headers["x-openmv-event"] == "webhook.ping" and _verify(new, req)["type"] == "webhook.ping"
    with pytest.raises(AssertionError):
        _verify(old, req)
    # the ping is queued once, whatever the subscription (device.* would not match it)
    assert store.query_one("SELECT COUNT(*) AS n FROM webhook_deliveries WHERE event = 'webhook.ping'")["n"] == 1
    # unknown delivery id on retry; the endpoint cap; delete takes the history with it
    assert c.post(f"/api/v1/admin/webhooks/{wid}/deliveries/dl_nope/retry", headers=AUTH).status_code == 404
    for i in range(wh.MAX_ENDPOINTS - 1):
        assert c.post("/api/v1/admin/webhooks", headers=AUTH, json={"url": f"https://ok{i}.example/"}).status_code == 200
    assert c.post("/api/v1/admin/webhooks", headers=AUTH, json={"url": "https://one-too-many.example/"}).status_code == 409
    assert c.delete(f"/api/v1/admin/webhooks/{wid}", headers=AUTH).json()["deleted"] is True
    assert store.query_one("SELECT COUNT(*) AS n FROM webhook_deliveries WHERE webhook_id = ?", (wid,))["n"] == 0
    assert c.get(f"/api/v1/admin/webhooks/{wid}", headers=AUTH).status_code == 404


def test_a_self_host_may_allow_private_endpoints(tmp_path, monkeypatch):
    c, store, rx, deliverer = _app(tmp_path, monkeypatch, webhook_allow_private=True)
    monkeypatch.setattr(wh.socket, "getaddrinfo", lambda h, p: [(0, 0, 0, "", ("127.0.0.1", p))])
    assert c.post("/api/v1/admin/webhooks", headers=AUTH, json={"url": "http://localhost:9000/hook"}).status_code == 200


def test_event_matching_and_the_catalogue():
    m = SqliteMetadataStore.event_matches
    assert m(["*"], "anything.at.all") and m(["rollout.*"], "rollout.stop") and m(["rollout.stop"], "rollout.stop")
    assert not m(["rollout.*"], "device.forget") and not m(["rollout.stop"], "rollout.create")
    assert not m(["rollout"], "rollout.stop")
    assert wh.MAX_ATTEMPTS == len(wh.BACKOFF_S) + 1 and all(a < b for a, b in itertools.pairwise(wh.BACKOFF_S))
    for name in ("device.enrolled", "install.failed", "device.fallback", "advisory.found", "webhook.ping",
                 "device.registered", "device.released", "device.checkin_refused", "desk_key.put"):
        assert name in wh.EVENTS


def test_the_device_path_raises_its_own_events(tmp_path, monkeypatch):
    """Enrolment, a fallback boot and a failed install come off the device endpoints, which
    the audit never recorded before -- now they are events a subscriber can act on."""
    c, store, rx, deliverer = _app(tmp_path, monkeypatch)
    c.post("/api/v1/admin/webhooks", headers=AUTH, json={"url": "https://hooks.example/", "events": ["device.*", "install.*"]})
    camera.register(store, "OPENMV_N6:aa", "a1")                      # the camera is the account's
    store.bind_device_account("OPENMV_N6:aa", "a1", source="admin")
    checkin = {"device_id": "aa", "product_id": 7, "board": "OPENMV_N6", "app_version": "1.0.0",
               "payload_version": 1, "slot": "a", "confirmed": True}
    assert camera.check(c, checkin).status_code == 200
    assert camera.check(c, checkin).status_code == 200            # no second enrolment
    assert camera.check(c, {**checkin, "fallback_reason": "boot-loop"}).status_code == 200
    assert camera.check(c, {**checkin, "fallback_reason": "boot-loop"}).status_code == 200  # same reason: once
    store.add_release(release_id="rel_1", product_id=7, product="P", version="2.0.0",
                      payload_version=2, min_platform_version=0, image_sha256="ab" * 32,
                      image_size=1, representations=[], manifest_key="m", image_key="i",
                      account_id="a1")                                # the report is about a release of its account
    assert camera.feedback(c, {"device_id": "aa", "product_id": 7, "board": "OPENMV_N6",
                               "release_id": "rel_1", "status": "failed", "reason": "sha"}).json()["ok"]
    actions = [e["action"] for e in c.get("/api/v1/admin/audit", headers=AUTH).json()["events"]]
    assert actions.count("device.enrolled") == 1 and actions.count("device.fallback") == 1
    assert actions.count("install.failed") == 1
    deliverer.run_once(now=time.time() + 1)
    assert sorted(r.headers["x-openmv-event"] for r in rx.calls) == ["device.enrolled", "device.fallback", "install.failed"]


def test_leftover_deliveries_for_a_switched_off_endpoint_die_quietly(tmp_path, monkeypatch):
    """A delivery leased after its endpoint was switched off (a race with the worker) is
    marked dead rather than sent; and an absurd URL is refused before DNS."""
    c, store, rx, deliverer = _app(tmp_path, monkeypatch)
    wid = c.post("/api/v1/admin/webhooks", headers=AUTH, json={"url": "https://hooks.example/"}).json()["webhook_id"]
    store.append_audit(actor="ci", action="cohort.create", entity_type="cohort", entity_id="x", account_id="a1")
    store.execute("UPDATE webhooks SET active = 0 WHERE webhook_id = ?", (wid,))   # off, deliveries left pending
    out = deliverer.run_once(now=time.time() + 1)
    assert out["dead"] >= 1 and out["sent"] == 0 and rx.calls == []
    with pytest.raises(wh.WebhookError, match="too long"):
        wh.check_url("https://x.example/" + "a" * 2100, resolve=_resolve_public)


def test_an_endpoint_is_checked_again_at_every_send(tmp_path, monkeypatch):
    """The address check ran when the endpoint was saved and never again, so a name that
    resolved somewhere public then could point inside the network at delivery. And the
    transport's own error text, which names hosts and ports, went back to the account."""
    c, store, rx, deliverer = _app(tmp_path, monkeypatch)
    hook = c.post("/api/v1/admin/webhooks", headers=AUTH,
                  json={"url": "https://hooks.example/", "events": ["token.*"]}).json()
    store.append_audit(actor="ci", action="token.issue", entity_type="token", entity_id="t",
                       account_id="a1")
    monkeypatch.setattr(wh.socket, "getaddrinfo",
                        lambda h, p: [(None, None, None, None, ("10.0.0.7", p))])
    deliverer.run_once(now=time.time() + 1)
    assert rx.calls == []                                               # never sent
    rows = c.get("/api/v1/admin/webhooks/%s/deliveries" % hook["webhook_id"],
                 headers=AUTH).json()["deliveries"]
    assert [r["last_error"] for r in rows] == ["the host resolves to a private address"]
    # a transport failure is reported by its kind alone
    monkeypatch.setattr(wh.socket, "getaddrinfo", _resolve_public)
    rx.raise_error = True
    deliverer.run_once(now=time.time() + 3600)
    rows = c.get("/api/v1/admin/webhooks/%s/deliveries" % hook["webhook_id"],
                 headers=AUTH).json()["deliveries"]
    assert rows[0]["last_error"] == "ConnectError"


@pytest.mark.parametrize("addr", ["100.64.0.1", "10.1.2.3", "127.0.0.1", "169.254.169.254",
                                  "192.168.1.1", "::1", "fd00::1", "0.0.0.0", "224.0.0.1"])
def test_an_address_that_is_not_public_is_refused(addr):
    """100.64.0.0/10 is the one a list of the private kinds misses."""
    with pytest.raises(wh.WebhookError, match="private address"):
        wh.check_url("https://x.example/", resolve=lambda h, p: [(0, 0, 0, 0, (addr, p))])


def test_a_delivery_connects_to_the_address_it_checked():
    """Checking the name and then handing the NAME to the HTTP client is two lookups: one
    that answers public for the check and private for the connection got through."""
    answers = iter([[(0, 0, 0, 0, ("93.184.216.34", 443))],      # the check
                    [(0, 0, 0, 0, ("10.0.0.7", 443))]])          # the lookup that is used
    with pytest.raises(wh.WebhookError, match="private address"):
        wh.pinned("https://rebind.example/h", resolve=lambda h, p: next(answers))
    assert wh.pinned("https://x.example:8443/h?a=1", resolve=_resolve_public) == (
        "https://93.184.216.34:8443/h?a=1", "x.example:8443", "x.example")
    v6 = lambda h, p: [(0, 0, 0, 0, ("2606:2800:220:1:248:1893:25c8:1946", p))]   # noqa: E731
    assert wh.pinned("https://x.example/h", resolve=v6)[0] == \
        "https://[2606:2800:220:1:248:1893:25c8:1946]/h"
    with pytest.raises(wh.WebhookError, match="does not resolve"):
        wh.pinned("https://x.example/h", resolve=lambda h, p: [] if p else None)


@pytest.mark.parametrize("addr", ["64:ff9b::a00:1", "64:ff9b::7f00:1", "64:ff9b::a9fe:a9fe", "::a00:1"])
def test_an_ipv6_wrapper_around_an_ipv4_address_is_not_public(addr):
    """NAT64 forwards 64:ff9b::/96 to the IPv4 address inside, private or not."""
    with pytest.raises(wh.WebhookError, match="private address"):
        wh.check_url("https://x.example/", resolve=lambda h, p: [(0, 0, 0, 0, (addr, p))])
    assert wh._public(__import__("ipaddress").ip_address("2606:2800:220:1:248:1893:25c8:1946"))


def test_a_delivery_reads_the_status_and_none_of_the_answer(tmp_path, monkeypatch):
    """The endpoint's answer was read whole, and inflated: a small gzip body was as much
    memory as its owner liked, in the server's process."""
    c, store, rx, deliverer = _app(tmp_path, monkeypatch)
    read = []

    class _Body(httpx.SyncByteStream):
        def __iter__(self):
            read.append(1)
            yield b"x" * 1024

    def endpoint(request):
        rx.calls.append(request)
        return httpx.Response(200, stream=_Body())
    deliverer._http = httpx.Client(transport=httpx.MockTransport(endpoint))
    c.post("/api/v1/admin/webhooks", headers=AUTH, json={"url": "https://hooks.example/", "events": ["token.*"]})
    store.append_audit(actor="ci", action="token.issue", entity_type="token", entity_id="t", account_id="a1")
    assert deliverer.run_once(now=time.time() + 1)["sent"] == 1
    assert read == [] and rx.calls[-1].headers["accept-encoding"] == "identity"


def test_a_name_that_stops_resolving_between_the_check_and_the_send_is_refused():
    answers = iter([[(0, 0, 0, 0, ("93.184.216.34", 443))]])

    def once(h, p):
        try:
            return next(answers)
        except StopIteration:
            raise OSError("gone") from None
    with pytest.raises(wh.WebhookError, match="does not resolve"):
        wh.pinned("https://x.example/h", resolve=once)

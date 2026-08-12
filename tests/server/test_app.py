"""The device-facing API + capability gateway, via FastAPI's TestClient."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from openmv_ota.server import capability
from openmv_ota.server.app import create_app
from openmv_ota.server.errors import ServerError
from openmv_ota.server.metastore import SqliteMetadataStore
from openmv_ota.server.settings import ServerSettings
from openmv_ota.server.storage import LocalArtifactStorage
from openmv_ota.server.verify import Registration

SECRET = "test-secret"


class _Verifier:
    def __init__(self, registered=True, registrar_ref="o1"):
        self._reg = Registration(registered, registrar_ref)
        self.calls = 0
        self.last_board = None

    def verify(self, board, device_id):
        self.calls += 1
        self.last_board = board
        return self._reg


def _app(tmp_path, *, registered=True, base_url="https://ota.test", rate=0, unverified=(),
         downgrades=False, cors=""):
    store = SqliteMetadataStore(str(tmp_path / "ota.db"))
    store.migrate()
    store.set_meta("cohort_salt", SECRET)
    storage = LocalArtifactStorage(str(tmp_path / "blobs"))
    settings = ServerSettings(base_url=base_url, checkin_rate_per_min=rate,
                              swd_ids_verify_url="u", swd_ids_verify_token="t",
                              unverified_boards=set(unverified),
                              test_offer_downgrades=downgrades,
                              cors_allow_origins=cors)
    verifier = _Verifier(registered)
    app = create_app(settings, storage=storage, metastore=store, verifier=verifier)
    return app, store, storage, verifier


BID = 7

def _seed(store, *, pv=0x02000000, percent=100, storage=None, manifest=b"MANI", image=b"IMG"):
    store.add_release(release_id="rel1", product_id=BID, product="P", version="2.0.0",
                      payload_version=pv, min_platform_version=0, image_sha256="ab" * 32,
                      image_size=len(image),
                      representations=[{"format": "full", "url": "OPENMV_N6-ota.img.gz",
                                        "size": len(image)}],
                      manifest_key="manifest/rel1", image_key="image/rel1")
    store.add_rollout(rollout_id="ro1", release_id="rel1", product_id=BID, cohort="__default__",
                      percent=percent)
    if storage is not None:
        storage.put("manifest/rel1", manifest, "application/octet-stream")
        storage.put("image/rel1", image, "application/gzip")


def _checkin(dev="dev1", product_id=BID, pv=0x01000000, **kw):
    return {"device_id": dev, "product_id": product_id, "payload_version": pv, **kw}


# --- health + validation --------------------------------------------------------------------

def test_healthz(tmp_path):
    app, *_ = _app(tmp_path)
    assert TestClient(app).get("/healthz").json() == {"ok": True}


def test_check_requires_device_id_and_board(tmp_path):
    app, *_ = _app(tmp_path)
    assert TestClient(app).post("/api/v1/check", json={"board": "X"}).status_code == 422


# --- the registration gate + zero footprint -------------------------------------------------

def test_unregistered_gets_nothing_and_writes_nothing(tmp_path):
    app, store, storage, v = _app(tmp_path, registered=False)
    _seed(store, percent=100)
    r = TestClient(app).post("/api/v1/check", json=_checkin())
    assert r.json() == {"update": False, "poll_after_s": 3600}
    assert store.get_device("dev1") is None                  # zero footprint
    assert store.get_rollout("ro1")["attempted"] == 0


def test_registered_no_rollout_writes_registry(tmp_path):
    app, store, storage, v = _app(tmp_path)
    assert TestClient(app).post("/api/v1/check", json=_checkin()).json()["update"] is False
    assert store.get_device("dev1") is not None


def test_firmware_board_translated_to_swd_ids_code(tmp_path):
    app, store, storage, v = _app(tmp_path)
    TestClient(app).post("/api/v1/check", json=_checkin(board="OPENMV_N6"))
    assert v.last_board == "N6"                              # verify() got the swd-ids code, not OPENMV_N6
    assert store.get_device("dev1")["board"] == "OPENMV_N6"  # the raw firmware name is still stored


def test_unverified_board_served_readonly_zero_footprint(tmp_path):
    # verifier would say NO, but a bypassed board is served anyway — read-only, no writes.
    app, store, storage, v = _app(tmp_path, registered=False, unverified=["ARDUINO_GIGA"])
    _seed(store, storage=storage, percent=100)
    r = TestClient(app).post("/api/v1/check", json=_checkin(board="ARDUINO_GIGA"))
    assert r.json()["update"] is True                        # got the update
    assert v.calls == 0                                      # verify was skipped
    assert store.get_device("dev1") is None                 # zero footprint — no device row
    assert store.get_rollout("ro1")["attempted"] == 0       # and no rollout accounting


def test_unverified_board_no_rollout_returns_nothing(tmp_path):
    app, store, *_ = _app(tmp_path, unverified=["ARDUINO_GIGA"])
    r = TestClient(app).post("/api/v1/check", json=_checkin(board="ARDUINO_GIGA"))
    assert r.json() == {"update": False, "poll_after_s": 3600}
    assert store.get_device("dev1") is None


# --- A/B slots: what the device reports, and what the server does with it --------------------

_TRIAL = [{"slot": "B", "running": True, "payload_version": 0x01000000, "counter": 5,
           "pending": True, "confirmed": False},
          {"slot": "A", "running": False, "payload_version": 0x00FF0000, "counter": 4,
           "pending": True, "confirmed": True}]
_SETTLED = [{"slot": "B", "running": True, "payload_version": 0x01000000, "counter": 5,
             "pending": True, "confirmed": True},
            {"slot": "A", "running": False, "payload_version": 0x00FF0000, "counter": 4,
             "pending": True, "confirmed": True}]


def test_mid_trial_device_is_not_offered_an_update(tmp_path):
    """It would be offered a release it is going to refuse anyway (the device defers too), and
    taking it would overwrite the confirmed image that is its only fallback."""
    app, store, storage, v = _app(tmp_path)
    _seed(store, storage=storage, percent=100)
    c = TestClient(app)
    assert c.post("/api/v1/check", json=_checkin(slots=_TRIAL)).json()["update"] is False
    # ...and the moment it confirms, the same rollout reaches it
    assert c.post("/api/v1/check", json=_checkin(slots=_SETTLED)).json()["update"] is True


def test_mid_trial_device_is_not_offered_a_pin_either(tmp_path):
    app, store, storage, v = _app(tmp_path)
    _seed(store, storage=storage, percent=0)                 # rollout reaches nobody
    store.set_cohort_pin(BID, "__default__", "rel1")         # ...but a pin would
    c = TestClient(app)
    assert c.post("/api/v1/check", json=_checkin(slots=_TRIAL)).json()["update"] is False
    assert c.post("/api/v1/check", json=_checkin(slots=_SETTLED)).json()["release_id"] == "rel1"


def test_fallback_version_is_recorded_and_survives_a_silent_checkin(tmp_path):
    app, store, storage, v = _app(tmp_path)
    c = TestClient(app)
    c.post("/api/v1/check", json=_checkin(slots=_SETTLED))
    assert store.get_device("dev1")["fallback_payload_version"] == 0x00FF0000
    # a later check-in that says nothing about slots keeps what we were last told, rather than
    # blanking the operator's view of the fleet
    c.post("/api/v1/check", json=_checkin())
    assert store.get_device("dev1")["fallback_payload_version"] == 0x00FF0000


def test_a_device_that_reports_no_slots_is_still_offered_updates(tmp_path):
    """The gate protects a fallback. A single-image device has none and must not be held back."""
    app, store, storage, v = _app(tmp_path)
    _seed(store, storage=storage, percent=100)
    assert TestClient(app).post("/api/v1/check", json=_checkin()).json()["update"] is True


# --- version pins (override rollouts) -------------------------------------------------------

def _seed_rel2(store):
    store.add_release(release_id="rel2", product_id=BID, product="P", version="3.0.0",
                      payload_version=0x03000000, min_platform_version=0, image_sha256="cd" * 32,
                      image_size=5, representations=[{"format": "full", "url": "x.img.gz", "size": 4}],
                      manifest_key="m/rel2", image_key="i/rel2")


def test_device_pin_overrides_rollout(tmp_path):
    app, store, storage, v = _app(tmp_path)
    _seed(store, storage=storage, percent=100)               # rollout offers rel1
    _seed_rel2(store)
    c = TestClient(app)
    assert c.post("/api/v1/check", json=_checkin()).json()["release_id"] == "rel1"
    store.set_device_pin("dev1", "rel2")                     # pin this device to a different release
    assert c.post("/api/v1/check", json=_checkin()).json()["release_id"] == "rel2"


def test_cohort_pin_offers_despite_zero_percent(tmp_path):
    app, store, storage, v = _app(tmp_path)
    _seed(store, storage=storage, percent=0)                 # 0% -> the rollout offers nobody
    store.set_cohort_pin(BID, "__default__", "rel1")
    assert TestClient(app).post("/api/v1/check", json=_checkin()).json()["release_id"] == "rel1"


def test_pin_to_current_holds(tmp_path):
    app, store, storage, v = _app(tmp_path)
    _seed(store, storage=storage, percent=100)
    store.set_cohort_pin(BID, "__default__", "rel1")         # pin to a release the device already runs
    r = TestClient(app).post("/api/v1/check", json=_checkin(pv=0x02000000))
    assert r.json() == {"update": False, "poll_after_s": 3600}   # held, rollout bypassed


def test_pin_to_unknown_release_holds(tmp_path):
    app, store, storage, v = _app(tmp_path)
    _seed(store, storage=storage, percent=100)
    store.set_cohort_pin(BID, "__default__", "ghost")
    assert TestClient(app).post("/api/v1/check", json=_checkin()).json() == {
        "update": False, "poll_after_s": 3600}


# --- POST /feedback (explicit terminal outcomes) --------------------------------------------

def _feedback(dev="dev1", product_id=BID, release_id="rel1", status="installed", **kw):
    return {"device_id": dev, "product_id": product_id, "release_id": release_id, "status": status, **kw}


def test_feedback_records_for_registered_device(tmp_path):
    app, store, *_ = _app(tmp_path)
    assert TestClient(app).post("/api/v1/feedback", json=_feedback()).json() == {"ok": True}
    assert store.deployment_counts("rel1") == {"installed": 1, "failed": 0}


def test_feedback_upserts_one_row_per_device_release(tmp_path):
    app, store, *_ = _app(tmp_path)
    c = TestClient(app)
    c.post("/api/v1/feedback", json=_feedback(status="installed"))
    c.post("/api/v1/feedback", json=_feedback(status="failed", reason="sha"))   # same (dev, rel)
    assert store.deployment_counts("rel1") == {"installed": 0, "failed": 1}      # overwritten, not doubled


def test_feedback_unregistered_is_noop(tmp_path):
    app, store, v = _app(tmp_path, registered=False)[:3]
    assert TestClient(app).post("/api/v1/feedback", json=_feedback()).json() == {"ok": False}
    assert store.deployment_counts("rel1") == {"installed": 0, "failed": 0}


def test_feedback_bypassed_board_is_noop(tmp_path):
    app, store, storage, v = _app(tmp_path, unverified=["ARDUINO_GIGA"])
    assert TestClient(app).post("/api/v1/feedback",
                                json=_feedback(board="ARDUINO_GIGA")).json() == {"ok": False}
    assert v.calls == 0                                       # bypass -> not verified, not recorded


def test_feedback_bad_status_400(tmp_path):
    app, *_ = _app(tmp_path)
    assert TestClient(app).post("/api/v1/feedback", json=_feedback(status="weird")).status_code == 400


def test_feedback_rate_limited(tmp_path):
    app, *_ = _app(tmp_path, rate=1)
    c = TestClient(app)
    c.post("/api/v1/feedback", json=_feedback())
    assert c.post("/api/v1/feedback", json=_feedback()).status_code == 429


# --- the rollout decision -------------------------------------------------------------------

def test_offer_mints_capability_url_and_accounts(tmp_path):
    app, store, storage, v = _app(tmp_path, base_url="https://ota.test/")
    _seed(store, pv=0x02000000, percent=100)
    body = TestClient(app).post("/api/v1/check", json=_checkin(pv=0x01000000)).json()
    assert body["update"] is True and body["release_id"] == "rel1"
    assert body["manifest_url"].startswith("https://ota.test/d/")
    assert body["manifest_url"].endswith("/manifest.bin")
    assert store.get_device("dev1")["last_offered_release_id"] == "rel1"
    assert store.get_rollout("ro1")["attempted"] == 1


def test_release_is_scoped_to_its_account(tmp_path):
    # a release+rollout published under account 'acctA' (same product_id) must reach only
    # acctA devices -- a device in acctB (or the '' self-host account) sees nothing.
    app, store, storage, v = _app(tmp_path)
    store.add_release(release_id="relA", product_id=BID, product="P", version="2.0.0",
                      payload_version=0x02000000, min_platform_version=0, image_sha256="ab" * 32,
                      image_size=3, representations=[{"format": "full", "url": "x.img.gz",
                                                      "size": 3}],
                      manifest_key="m/relA", image_key="i/relA", account_id="acctA")
    store.add_rollout(rollout_id="roA", release_id="relA", product_id=BID, cohort="__default__",
                      percent=100, account_id="acctA")
    c = TestClient(app)
    assert c.post("/api/v1/check", json=_checkin(dev="a", account_id="acctA")).json()["update"] is True
    assert c.post("/api/v1/check", json=_checkin(dev="b", account_id="acctB")).json()["update"] is False
    assert c.post("/api/v1/check", json=_checkin(dev="c")).json()["update"] is False   # '' account


def _seed_account_rollout(store, account="acctA"):
    store.add_release(release_id="relA", product_id=BID, product="P", version="2.0.0",
                      payload_version=0x02000000, min_platform_version=0, image_sha256="ab" * 32,
                      image_size=3, representations=[{"format": "full", "url": "x.img.gz", "size": 3}],
                      manifest_key="m/relA", image_key="i/relA", account_id=account)
    store.add_rollout(rollout_id="roA", release_id="relA", product_id=BID, cohort="__default__",
                      percent=100, account_id=account)


def test_account_binding_learn_sticky_and_golden_recovery(tmp_path):
    app, store, storage, v = _app(tmp_path)
    _seed_account_rollout(store, "acctA")
    c = TestClient(app)
    # LEARN: the first valid check-in binds the device to acctA and gets its release
    assert c.post("/api/v1/check", json=_checkin(dev="d", account_id="acctA")).json()["update"] is True
    assert store.device_account("d") == {"account_id": "acctA", "source": "learned"}
    # GOLDEN RECOVERY: a later fallback reporting '' still resolves to acctA (not stranded)
    assert c.post("/api/v1/check", json=_checkin(dev="d", account_id="")).json()["update"] is True
    assert store.device_account("d")["account_id"] == "acctA"          # sticky: not downgraded
    # SPOOF: a check-in claiming another account can't move the binding either
    c.post("/api/v1/check", json=_checkin(dev="d", account_id="acctEvil"))
    assert store.device_account("d")["account_id"] == "acctA"
    assert store.get_device("d")["account_id"] == "acctA"             # the row reflects the binding


def test_never_onboarded_device_stays_unbound(tmp_path):
    app, store, *_ = _app(tmp_path)
    TestClient(app).post("/api/v1/check", json=_checkin(dev="d", account_id=""))
    assert store.device_account("d") is None                          # only '' seen -> no binding


def test_unregistered_device_never_binds(tmp_path):
    app, store, *_ = _app(tmp_path, registered=False)
    TestClient(app).post("/api/v1/check", json=_checkin(dev="d", account_id="acctA"))
    assert store.device_account("d") is None                          # gate stands in front


def test_success_counted_when_device_runs_offered_release(tmp_path):
    app, store, storage, v = _app(tmp_path)
    _seed(store, pv=0x02000000, percent=100)
    c = TestClient(app)
    c.post("/api/v1/check", json=_checkin(pv=0x01000000))     # offered -> attempted 1
    assert store.get_rollout("ro1")["attempted"] == 1 and store.get_rollout("ro1")["updated"] == 0
    c.post("/api/v1/check", json=_checkin(pv=0x02000000))     # now running it -> updated 1
    assert store.get_rollout("ro1")["updated"] == 1


def test_not_in_staged_percent(tmp_path):
    app, store, storage, v = _app(tmp_path)
    _seed(store, pv=0x02000000, percent=0)
    assert TestClient(app).post("/api/v1/check", json=_checkin(pv=1)).json()["update"] is False


def test_anti_rollback_not_offered(tmp_path):
    app, store, storage, v = _app(tmp_path)
    _seed(store, pv=0x02000000, percent=100)
    assert TestClient(app).post("/api/v1/check", json=_checkin(pv=0x02000000)).json()["update"] is False


def test_test_offer_downgrades_offers_anti_rollback(tmp_path, capsys):
    # The TEST-ONLY hook: the server offers an equal/older release so the device's own
    # anti-rollback can be exercised on hardware. create_app also warns loudly when it is on.
    app, store, storage, v = _app(tmp_path, downgrades=True)
    assert "test_offer_downgrades is ON" in capsys.readouterr().err
    _seed(store, pv=0x02000000, percent=100, storage=storage)
    assert TestClient(app).post("/api/v1/check", json=_checkin(pv=0x02000000)).json()["update"] is True


def test_rollout_pointing_at_missing_release(tmp_path):
    app, store, storage, v = _app(tmp_path)
    store.add_rollout(rollout_id="ro1", release_id="ghost", product_id=BID,
                      cohort="__default__", percent=100)
    assert TestClient(app).post("/api/v1/check", json=_checkin(pv=1)).json()["update"] is False


def test_rate_limited(tmp_path):
    app, store, storage, v = _app(tmp_path, rate=1)
    c = TestClient(app)
    assert c.post("/api/v1/check", json=_checkin()).status_code == 200
    r = c.post("/api/v1/check", json=_checkin())
    assert r.status_code == 429 and r.headers["Retry-After"] == "3600"


def test_autopause_on_fallback_threshold(tmp_path):
    app, store, storage, v = _app(tmp_path)
    _seed(store, pv=0x02000000, percent=100)
    store.update_rollout("ro1", failure_threshold=0.4)
    c = TestClient(app)
    for d in ("d1", "d2", "d3"):                              # offered to 3 -> attempted 3
        c.post("/api/v1/check", json=_checkin(dev=d, pv=0x01000000))
    assert store.get_rollout("ro1")["attempted"] == 3
    for d in ("d1", "d2"):                                    # 2 fall back -> 2/3 > 0.4 -> paused
        c.post("/api/v1/check", json=_checkin(dev=d, pv=0x01000000, fallback_reason="crc"))
    ro = store.get_rollout("ro1")
    assert ro["failures"] == 2 and ro["state"] == "paused"
    assert any(e["action"] == "rollout.autopause" for e in store.read_audit())


# --- the capability gateway -----------------------------------------------------------------

def test_gateway_streams_local_artifacts(tmp_path):
    app, store, storage, v = _app(tmp_path)
    _seed(store, storage=storage, manifest=b"MANIFEST", image=b"IMAGE")
    c = TestClient(app)
    tok = capability.mint(SECRET, "rel1")
    m = c.get("/d/%s/manifest.bin" % tok)
    assert m.status_code == 200 and m.content == b"MANIFEST"
    assert m.headers["content-type"] == "application/octet-stream"
    i = c.get("/d/%s/OPENMV_N6-ota.img.gz" % tok)
    assert i.status_code == 200 and i.content == b"IMAGE"
    assert i.headers["content-type"] == "application/gzip"


def test_gateway_serves_byte_ranges(tmp_path):
    # A device on a poor link cannot finish a long download in one connection (the WINC1500 aborts
    # every transfer at ~50 s), so the installer RESUMES at the compressed offset it reached rather
    # than restarting. That needs ranges. Prod already has them -- it 302s to object storage -- so
    # this is the self-hosted path, which is also what the HIL bench runs: without it, resume could
    # not be tested on the one rig that reproduces the failure.
    app, store, storage, v = _app(tmp_path)
    _seed(store, storage=storage, manifest=b"MANIFEST", image=b"0123456789")
    c = TestClient(app)
    url = "/d/%s/OPENMV_N6-ota.img.gz" % capability.mint(SECRET, "rel1")

    full = c.get(url)
    assert full.status_code == 200 and full.content == b"0123456789"
    assert full.headers["accept-ranges"] == "bytes"      # advertised even without a Range

    r = c.get(url, headers={"Range": "bytes=4-"})        # the resume case: an open-ended suffix
    assert r.status_code == 206 and r.content == b"456789"
    assert r.headers["content-range"] == "bytes 4-9/10"

    r = c.get(url, headers={"Range": "bytes=2-5"})       # a closed range
    assert r.status_code == 206 and r.content == b"2345"
    assert r.headers["content-range"] == "bytes 2-5/10"

    # Past the end -> 416 with the true length, so a device that over-resumes learns the real size
    # instead of silently receiving nothing and writing a truncated image.
    r = c.get(url, headers={"Range": "bytes=99-"})
    assert r.status_code == 416 and r.headers["content-range"] == "bytes */10"

    # A range we do not implement (multi-range) must fall back to the WHOLE body, never a partial
    # one: answering a multi-range request with one arbitrary slice would corrupt the download.
    r = c.get(url, headers={"Range": "bytes=0-1,5-6"})
    assert r.status_code == 200 and r.content == b"0123456789"


def test_gateway_bad_token_404(tmp_path):
    app, *_ = _app(tmp_path)
    assert TestClient(app).get("/d/not-a-token/manifest.bin").status_code == 404


def test_gateway_unknown_filename_404(tmp_path):
    app, store, storage, v = _app(tmp_path)
    _seed(store, storage=storage)
    tok = capability.mint(SECRET, "rel1")
    assert TestClient(app).get("/d/%s/other.bin" % tok).status_code == 404


def test_gateway_missing_release_404(tmp_path):
    app, *_ = _app(tmp_path)
    tok = capability.mint(SECRET, "gone")
    assert TestClient(app).get("/d/%s/manifest.bin" % tok).status_code == 404


def test_gateway_missing_blob_404(tmp_path):
    app, store, storage, v = _app(tmp_path)
    _seed(store)                                              # release row but no blobs stored
    tok = capability.mint(SECRET, "rel1")
    assert TestClient(app).get("/d/%s/manifest.bin" % tok).status_code == 404


class _RedirStorage(LocalArtifactStorage):
    def url_for(self, key, *, expires=300):
        return "https://s3/%s" % key


def test_gateway_redirects_to_presigned(tmp_path):
    app, store, storage, v = _app(tmp_path)
    _seed(store, storage=storage)
    app.state.storage = _RedirStorage(str(tmp_path / "blobs"))
    tok = capability.mint(SECRET, "rel1")
    r = TestClient(app).get("/d/%s/manifest.bin" % tok, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "https://s3/manifest/rel1"


# --- create_app factory ---------------------------------------------------------------------

def test_create_app_requires_secret(tmp_path):
    store = SqliteMetadataStore(":memory:")
    store.migrate()                                          # no cohort_salt seeded
    with pytest.raises(ServerError, match="no server secret"):
        create_app(ServerSettings(swd_ids_verify_url="u", swd_ids_verify_token="t"),
                   metastore=store, storage=LocalArtifactStorage(str(tmp_path)), verifier=_Verifier())


def test_create_app_default_admin_auth_is_token_auth(tmp_path):
    from openmv_ota.server.auth import TokenAuth
    app, *_ = _app(tmp_path)
    assert isinstance(app.state.admin_auth, TokenAuth)


def test_create_app_injected_admin_auth(tmp_path):
    sentinel = object()
    store = SqliteMetadataStore(":memory:")
    store.migrate()
    store.set_meta("cohort_salt", "x")
    app = create_app(ServerSettings(swd_ids_verify_url="u", swd_ids_verify_token="t"),
                     metastore=store, storage=LocalArtifactStorage(str(tmp_path)),
                     verifier=_Verifier(), admin_auth=sentinel)
    assert app.state.admin_auth is sentinel


def test_create_app_builds_defaults(tmp_path):
    s = SqliteMetadataStore(str(tmp_path / "ota.db"))
    s.migrate()
    s.set_meta("cohort_salt", "x")
    s.close()
    settings = ServerSettings(database_url="sqlite:///" + str(tmp_path / "ota.db"),
                              storage_location=str(tmp_path / "blobs"),
                              swd_ids_verify_url="u", swd_ids_verify_token="t")
    app = create_app(settings)                               # builds storage/metastore/verifier
    assert TestClient(app).get("/healthz").json() == {"ok": True}


# --- CORS -------------------------------------------------------------------------------------
# A browser UI on another origin cannot read this API without it, and a wrong default here is a
# security bug in both directions: too open and any page can read admin responses, too closed and
# the UI silently fails. So both states are pinned.

def test_cors_is_off_by_default(tmp_path):
    """No allowlist configured -> no CORS headers at all, so a cross-origin page cannot read us."""
    app, _, _, _ = _app(tmp_path)
    r = TestClient(app).get("/healthz", headers={"Origin": "https://evil.test"})
    assert r.status_code == 200
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


def test_cors_allows_only_the_configured_origins(tmp_path):
    """A named origin is echoed back; anything else gets nothing -- no wildcard fallback."""
    app, _, _, _ = _app(tmp_path, cors="https://cloud.openmv.io, https://staging.openmv.io")
    c = TestClient(app)
    r = c.get("/healthz", headers={"Origin": "https://cloud.openmv.io"})
    assert r.headers["access-control-allow-origin"] == "https://cloud.openmv.io"
    r = c.get("/healthz", headers={"Origin": "https://evil.test"})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


def test_cors_refuses_a_wildcard_origin(tmp_path):
    """`*` must not boot. Starlette reads a "*" in allow_origins as allow-all, so a setting
    documented as "name your origins" would otherwise hand a wildcard to anyone who typed the
    obvious thing -- and that is a silent hole, not a visible one."""
    with pytest.raises(ServerError) as e:
        _app(tmp_path, cors="https://cloud.openmv.io, *")
    assert "does not accept '*'" in str(e.value)


def test_cors_preflight_permits_the_admin_verbs_and_bearer_header(tmp_path):
    """The UI sends `Authorization: Bearer ...` on PATCH/DELETE, so the preflight must allow both,
    and must NOT allow credentials -- this API is token-authenticated, never cookie-authenticated."""
    app, _, _, _ = _app(tmp_path, cors="https://cloud.openmv.io")
    r = TestClient(app).options("/api/v1/admin/rollouts", headers={
        "Origin": "https://cloud.openmv.io",
        "Access-Control-Request-Method": "PATCH",
        "Access-Control-Request-Headers": "authorization",
    })
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "https://cloud.openmv.io"
    assert "PATCH" in r.headers["access-control-allow-methods"]
    assert "authorization" in r.headers["access-control-allow-headers"].lower()
    assert "access-control-allow-credentials" not in {k.lower() for k in r.headers}

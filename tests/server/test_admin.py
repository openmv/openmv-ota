"""The admin API: rollout control (auth + scopes + audit) and fleet observability."""

from __future__ import annotations

from fastapi.testclient import TestClient

from openmv_ota.server.app import create_app
from openmv_ota.server.auth import hash_token
from openmv_ota.server.metastore import SqliteMetadataStore
from openmv_ota.server.settings import ServerSettings
from openmv_ota.server.storage import LocalArtifactStorage
from openmv_ota.server.verify import Registration


class _Verifier:
    def verify(self, board, device_id):
        return Registration(True)


def _app(tmp_path, scopes=("rollout:control", "fleet:read")):
    store = SqliteMetadataStore(str(tmp_path / "ota.db"))
    store.migrate()
    store.set_meta("cohort_salt", "x")
    store.add_token(hash_token("admintok"), "ci", list(scopes))
    app = create_app(ServerSettings(base_url="https://ota.test", swd_ids_verify_url="u",
                                    swd_ids_verify_token="t"),
                     metastore=store, storage=LocalArtifactStorage(str(tmp_path / "blobs")),
                     verifier=_Verifier())
    return app, store


AUTH = {"Authorization": "Bearer admintok"}


BID = 7

def _seed_release(store, rid="rel1", product_id=BID, pv=0x02000000):
    store.add_release(release_id=rid, product_id=product_id, product="P", version="2.0.0",
                      payload_version=pv, min_platform_version=0, image_sha256="ab" * 32,
                      image_size=10, representations=[{"format": "full", "url": "x.img.gz",
                                                       "size": 9}],
                      manifest_key="m/%s" % rid, image_key="i/%s" % rid)


# --- auth + scopes --------------------------------------------------------------------------

def test_no_token_401(tmp_path):
    app, store = _app(tmp_path)
    assert TestClient(app).post("/api/v1/admin/rollouts",
                                json={"release_id": "x", "percent": 5}).status_code == 401


def test_wrong_scope_403(tmp_path):
    app, store = _app(tmp_path, scopes=("fleet:read",))       # can't control rollouts
    _seed_release(store)
    r = TestClient(app).post("/api/v1/admin/rollouts", headers=AUTH,
                             json={"release_id": "rel1", "percent": 5})
    assert r.status_code == 403


# --- cohorts --------------------------------------------------------------------------------

def test_cohorts_list_and_assign(tmp_path):
    app, store = _app(tmp_path)
    store.upsert_device(device_id="d1", product_id=BID)
    store.upsert_device(device_id="d2", product_id=BID)
    c = TestClient(app)
    assert c.get("/api/v1/admin/cohorts", headers=AUTH).json() == {
        "cohorts": [{"cohort": "__default__", "devices": 2}]}
    r = c.post("/api/v1/admin/cohorts/assign", headers=AUTH,
               json={"cohort": "beta", "device_ids": ["d1", "ghost"]})   # ghost doesn't exist
    assert r.json() == {"cohort": "beta", "assigned": 1}                 # only d1 was updated
    got = {x["cohort"]: x["devices"]
           for x in c.get("/api/v1/admin/cohorts", headers=AUTH).json()["cohorts"]}
    assert got == {"__default__": 1, "beta": 1}
    empty = c.post("/api/v1/admin/cohorts/assign", headers=AUTH,
                   json={"cohort": "beta", "device_ids": []})
    assert empty.json() == {"cohort": "beta", "assigned": 0}   # no-op when nothing to assign


def test_cohort_assign_requires_scope(tmp_path):
    app, store = _app(tmp_path, scopes=("fleet:read",))
    r = TestClient(app).post("/api/v1/admin/cohorts/assign", headers=AUTH,
                             json={"cohort": "b", "device_ids": ["x"]})
    assert r.status_code == 403


# --- pins -----------------------------------------------------------------------------------

def test_device_pin_set_and_clear(tmp_path):
    app, store = _app(tmp_path)
    store.upsert_device(device_id="d1", product_id=BID)
    c = TestClient(app)
    assert c.patch("/api/v1/admin/devices/d1/pin", headers=AUTH,
                   json={"release_id": "rel1"}).json() == {"device_id": "d1", "pinned_release_id": "rel1"}
    assert store.get_device("d1")["pinned_release_id"] == "rel1"
    c.patch("/api/v1/admin/devices/d1/pin", headers=AUTH, json={"release_id": None})   # unpin
    assert store.get_device("d1")["pinned_release_id"] is None


def test_device_pin_404_when_missing(tmp_path):
    app, store = _app(tmp_path)
    assert TestClient(app).patch("/api/v1/admin/devices/ghost/pin", headers=AUTH,
                                 json={"release_id": "r"}).status_code == 404


def test_cohort_pin_set_and_clear(tmp_path):
    app, store = _app(tmp_path)
    c = TestClient(app)
    c.post("/api/v1/admin/cohorts/pin", headers=AUTH,
           json={"product_id": BID, "cohort": "beta", "release_id": "rel1"})
    assert store.get_cohort_pin(BID, "beta") == "rel1"
    c.post("/api/v1/admin/cohorts/pin", headers=AUTH,
           json={"product_id": BID, "cohort": "beta", "release_id": None})
    assert store.get_cohort_pin(BID, "beta") is None


def test_pin_requires_scope(tmp_path):
    app, store = _app(tmp_path, scopes=("fleet:read",))
    store.upsert_device(device_id="d1", product_id=BID)
    assert TestClient(app).patch("/api/v1/admin/devices/d1/pin", headers=AUTH,
                                 json={"release_id": "r"}).status_code == 403


# --- create rollout -------------------------------------------------------------------------

def test_create_rollout(tmp_path):
    app, store = _app(tmp_path)
    _seed_release(store)
    r = TestClient(app).post("/api/v1/admin/rollouts", headers=AUTH,
                             json={"release_id": "rel1", "percent": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["product_id"] == BID and body["state"] == "active" and body["percent"] == 5
    assert store.get_rollout(body["rollout_id"])["release_id"] == "rel1"
    assert any(e["action"] == "rollout.create" for e in store.read_audit())


def test_create_rollout_missing_release_404(tmp_path):
    app, store = _app(tmp_path)
    r = TestClient(app).post("/api/v1/admin/rollouts", headers=AUTH,
                             json={"release_id": "ghost", "percent": 5})
    assert r.status_code == 404


def test_create_rollout_supersedes_active(tmp_path):
    app, store = _app(tmp_path)
    _seed_release(store, rid="rel1", pv=0x02000000)
    _seed_release(store, rid="rel2", pv=0x02010000)
    c = TestClient(app)
    first = c.post("/api/v1/admin/rollouts", headers=AUTH,
                   json={"release_id": "rel1", "percent": 5}).json()["rollout_id"]
    c.post("/api/v1/admin/rollouts", headers=AUTH, json={"release_id": "rel2", "percent": 5})
    assert store.get_rollout(first)["state"] == "paused"      # the older active was superseded
    assert any(e["action"] == "rollout.superseded" for e in store.read_audit())


# --- patch / rollback -----------------------------------------------------------------------

def _make_rollout(c, store):
    _seed_release(store)
    return c.post("/api/v1/admin/rollouts", headers=AUTH,
                  json={"release_id": "rel1", "percent": 10}).json()["rollout_id"]


def test_patch_raise_percent_and_pause_resume(tmp_path):
    app, store = _app(tmp_path)
    c = TestClient(app)
    rid = _make_rollout(c, store)
    assert c.patch("/api/v1/admin/rollouts/%s" % rid, headers=AUTH,
                   json={"percent": 50}).json()["percent"] == 50
    assert c.patch("/api/v1/admin/rollouts/%s" % rid, headers=AUTH,
                   json={"state": "paused"}).json()["state"] == "paused"


def test_patch_rejects_lowering_percent(tmp_path):
    app, store = _app(tmp_path)
    c = TestClient(app)
    rid = _make_rollout(c, store)
    assert c.patch("/api/v1/admin/rollouts/%s" % rid, headers=AUTH,
                   json={"percent": 5}).status_code == 400


def test_patch_bad_state_and_empty_and_missing(tmp_path):
    app, store = _app(tmp_path)
    c = TestClient(app)
    rid = _make_rollout(c, store)
    assert c.patch("/api/v1/admin/rollouts/%s" % rid, headers=AUTH,
                   json={"state": "weird"}).status_code == 400
    assert c.patch("/api/v1/admin/rollouts/%s" % rid, headers=AUTH, json={}).status_code == 400
    assert c.patch("/api/v1/admin/rollouts/nope", headers=AUTH,
                   json={"percent": 90}).status_code == 404


def test_rollback(tmp_path):
    app, store = _app(tmp_path)
    c = TestClient(app)
    rid = _make_rollout(c, store)
    assert c.post("/api/v1/admin/rollouts/%s/rollback" % rid,
                  headers=AUTH).json()["state"] == "rolled_back"
    assert store.get_rollout(rid)["state"] == "rolled_back"
    assert c.post("/api/v1/admin/rollouts/nope/rollback", headers=AUTH).status_code == 404


# --- observability --------------------------------------------------------------------------

def test_list_rollouts_and_status(tmp_path):
    app, store = _app(tmp_path)
    c = TestClient(app)
    rid = _make_rollout(c, store)
    assert [r["rollout_id"] for r in c.get("/api/v1/admin/rollouts", headers=AUTH).json()
            ["rollouts"]] == [rid]
    st = c.get("/api/v1/admin/rollouts/%s/status" % rid, headers=AUTH).json()
    assert st["success_rate"] is None                        # no attempts yet
    store.bump_rollout(rid, attempted=4, updated=3)
    st2 = c.get("/api/v1/admin/rollouts/%s/status" % rid, headers=AUTH).json()
    assert st2["attempted"] == 4 and st2["updated"] == 3 and st2["success_rate"] == 0.75
    assert c.get("/api/v1/admin/rollouts/nope/status", headers=AUTH).status_code == 404


def test_list_releases(tmp_path):
    app, store = _app(tmp_path)
    _seed_release(store, rid="rel1", pv=0x02000000)
    _seed_release(store, rid="rel2", pv=0x02010000)
    c = TestClient(app)
    got = c.get("/api/v1/admin/releases", headers=AUTH).json()["releases"]
    assert {r["release_id"] for r in got} == {"rel1", "rel2"}
    assert got[0]["representations"][0]["format"] == "full"          # json-decoded, not a string
    assert [r["release_id"] for r in
            c.get("/api/v1/admin/releases?product_id=999", headers=AUTH).json()["releases"]] == []


def test_devices_cohort_filter_and_paging(tmp_path):
    app, store = _app(tmp_path)
    for i in range(3):
        store.upsert_device(device_id="d%d" % i, product_id=BID, cohort="beta")
    store.upsert_device(device_id="x", product_id=BID, cohort="__default__")
    c = TestClient(app)
    beta = c.get("/api/v1/admin/devices?cohort=beta", headers=AUTH).json()["devices"]
    assert {d["device_id"] for d in beta} == {"d0", "d1", "d2"}     # cohort filter
    assert len(c.get("/api/v1/admin/devices?limit=2", headers=AUTH).json()["devices"]) == 2
    assert len(c.get("/api/v1/admin/devices?limit=2&offset=2",     # 4 total -> 2 left on page 2
                     headers=AUTH).json()["devices"]) == 2


def test_fleet_devices_audit(tmp_path):
    app, store = _app(tmp_path)
    store.upsert_device(device_id="d1", product_id=BID, board="OPENMV_N6", current_version="1.0.0",
                        slot="FRONT")
    store.append_audit(actor="ci", action="release.publish", entity_type="release", entity_id="r1")
    c = TestClient(app)
    assert c.get("/api/v1/admin/fleet", headers=AUTH).json()["total"] == 1
    assert c.get("/api/v1/admin/devices", headers=AUTH).json()["devices"][0]["device_id"] == "d1"
    events = c.get("/api/v1/admin/audit", headers=AUTH).json()["events"]
    assert events[0]["action"] == "release.publish"


# --- account isolation (adversarial: B must never see or touch A's data) --------------------

def _two_accounts(tmp_path):
    store = SqliteMetadataStore(str(tmp_path / "ota.db"))
    store.migrate()
    store.set_meta("cohort_salt", "x")
    for acc in ("acctA", "acctB"):
        store.add_account(acc, acc)
        store.add_token(hash_token("tok" + acc[-1]), acc,
                        ["release:write", "rollout:control", "fleet:read"], account_id=acc)
    app = create_app(ServerSettings(base_url="https://ota.test", swd_ids_verify_url="u",
                                    swd_ids_verify_token="t"),
                     metastore=store, storage=LocalArtifactStorage(str(tmp_path / "blobs")),
                     verifier=_Verifier())
    return app, store


def _seed_for(store, account, rid, pv=0x02000000):
    store.add_release(release_id=rid, product_id=BID, product="P", version="2.0.0",
                      payload_version=pv, min_platform_version=0, image_sha256="ab" * 32,
                      image_size=10, representations=[{"format": "full", "url": "x.img.gz",
                                                       "size": 9}],
                      manifest_key="m/%s" % rid, image_key="i/%s" % rid, account_id=account)


A = {"Authorization": "Bearer tokA"}
B = {"Authorization": "Bearer tokB"}


def test_injected_website_auth_scopes_by_account(tmp_path):
    # the website injects its own admin_auth that resolves identity -> account; the scoping must
    # honor whatever account that Principal carries (the hosted path, no admin_tokens rows).
    store = SqliteMetadataStore(str(tmp_path / "ota.db"))
    store.migrate()
    store.set_meta("cohort_salt", "x")
    store.upsert_device(device_id="dz", product_id=BID, account_id="acctZ")
    store.upsert_device(device_id="dq", product_id=BID, account_id="acctQ")

    class WebsiteAuth:
        def authenticate(self, authorization):
            from openmv_ota.server.auth import Principal
            return Principal(name="web-user", scopes=["fleet:read"], account_id="acctZ")

    app = create_app(ServerSettings(base_url="https://ota.test", swd_ids_verify_url="u",
                                    swd_ids_verify_token="t"),
                     metastore=store, storage=LocalArtifactStorage(str(tmp_path / "blobs")),
                     verifier=_Verifier(), admin_auth=WebsiteAuth())
    devs = TestClient(app).get("/api/v1/admin/devices", headers={"Authorization": "x"}).json()["devices"]
    assert [d["device_id"] for d in devs] == ["dz"]           # only acctZ, as the injected auth said


def test_account_isolation(tmp_path):
    app, store = _two_accounts(tmp_path)
    _seed_for(store, "acctA", "relA")
    _seed_for(store, "acctB", "relB")
    store.upsert_device(device_id="dA", product_id=BID, account_id="acctA")
    store.upsert_device(device_id="dB", product_id=BID, account_id="acctB")
    c = TestClient(app)

    # reads are scoped: A sees only its own device + fleet count
    assert [d["device_id"] for d in c.get("/api/v1/admin/devices", headers=A).json()["devices"]] == ["dA"]
    assert c.get("/api/v1/admin/fleet", headers=A).json()["total"] == 1

    # A creates a rollout on its release; B can neither see nor touch it (404, not 403 -> no leak)
    roA = c.post("/api/v1/admin/rollouts", headers=A,
                 json={"release_id": "relA", "percent": 5}).json()["rollout_id"]
    assert c.get("/api/v1/admin/rollouts", headers=B).json()["rollouts"] == []
    assert c.get("/api/v1/admin/rollouts/%s/status" % roA, headers=B).status_code == 404
    assert c.patch("/api/v1/admin/rollouts/%s" % roA, headers=B, json={"percent": 50}).status_code == 404
    assert c.post("/api/v1/admin/rollouts/%s/rollback" % roA, headers=B).status_code == 404

    # B cannot roll out, or pin its cohort to, A's release
    assert c.post("/api/v1/admin/rollouts", headers=B,
                  json={"release_id": "relA", "percent": 5}).status_code == 404
    assert c.post("/api/v1/admin/cohorts/pin", headers=B,
                  json={"product_id": BID, "cohort": "beta", "release_id": "relA"}).status_code == 404

    # B cannot pin or reassign A's device
    assert c.patch("/api/v1/admin/devices/dA/pin", headers=B,
                   json={"release_id": None}).status_code == 404
    assert c.post("/api/v1/admin/cohorts/assign", headers=B,
                  json={"cohort": "beta", "device_ids": ["dA"]}).json()["assigned"] == 0

    # audit is per-account: B sees only its OWN events (its cohort.assign), never A's rollout.create
    b_events = c.get("/api/v1/admin/audit", headers=B).json()["events"]
    assert b_events and all(e["action"] != "rollout.create" for e in b_events)
    assert any(e["action"] == "rollout.create" for e in
               c.get("/api/v1/admin/audit", headers=A).json()["events"])

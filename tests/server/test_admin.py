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


def _app(tmp_path, scopes=("manage", "observe")):
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
    app, store = _app(tmp_path, scopes=("observe",))       # can't control rollouts
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
    app, store = _app(tmp_path, scopes=("observe",))
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
    app, store = _app(tmp_path, scopes=("observe",))
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


def test_releases_and_rollouts_paging(tmp_path):
    app, store = _app(tmp_path)
    for i in range(3):
        _seed_release(store, rid="r%d" % i, pv=0x02000000 + i)
        store.add_rollout(rollout_id="ro%d" % i, release_id="r%d" % i, product_id=BID,
                          cohort="c%d" % i, percent=5)
    c = TestClient(app)
    assert len(c.get("/api/v1/admin/releases?limit=2", headers=AUTH).json()["releases"]) == 2
    assert len(c.get("/api/v1/admin/releases?limit=2&offset=2", headers=AUTH).json()["releases"]) == 1
    assert len(c.get("/api/v1/admin/rollouts?limit=1", headers=AUTH).json()["rollouts"]) == 1


def test_accounts_endpoint_create_list_and_scope(tmp_path):
    app, store = _app(tmp_path, scopes=("accounts",))          # a super-admin (operator) token
    c = TestClient(app)
    r = c.post("/api/v1/admin/accounts", headers=AUTH, json={"name": "DroneCo"})
    assert r.status_code == 200
    body = r.json()
    assert body["account_id"].startswith("acct_") and body["token"] and body["name"] == "DroneCo"
    assert store.get_account(body["account_id"])["name"] == "DroneCo"
    tok = store.get_token(hash_token(body["token"]))               # its token acts for that account,
    assert tok["account_id"] == body["account_id"] and "accounts" not in tok["scopes"]  # not privileged
    assert body["account_id"] in [a["account_id"]
                                  for a in c.get("/api/v1/admin/accounts", headers=AUTH).json()["accounts"]]


def test_accounts_endpoint_requires_super_admin(tmp_path):
    app, store = _app(tmp_path, scopes=("manage", "observe"))   # no accounts
    c = TestClient(app)
    assert c.post("/api/v1/admin/accounts", headers=AUTH, json={"name": "X"}).status_code == 403
    assert c.get("/api/v1/admin/accounts", headers=AUTH).status_code == 403


def test_token_management_api(tmp_path):
    app, store = _app(tmp_path, scopes=("accounts",))
    store.add_account("acctA", "A")
    c = TestClient(app)
    # issue with default (worker) scopes; the secret is returned exactly here
    body = c.post("/api/v1/admin/accounts/acctA/tokens", headers=AUTH, json={"name": "ci"}).json()
    assert body["scopes"] == ["publish", "manage", "observe"] and body["account_id"] == "acctA"
    th = body["token_hash"]
    assert body["token"] and store.get_token(th)["account_id"] == "acctA"
    # explicit scopes, a bad scope, and a missing account
    assert c.post("/api/v1/admin/accounts/acctA/tokens", headers=AUTH,
                  json={"name": "ro", "scopes": ["observe"]}).json()["scopes"] == ["observe"]
    assert c.post("/api/v1/admin/accounts/acctA/tokens", headers=AUTH,
                  json={"name": "x", "scopes": ["god"]}).status_code == 400
    assert c.post("/api/v1/admin/accounts/ghost/tokens", headers=AUTH,
                  json={"name": "x"}).status_code == 404
    # list is metadata only -- never the secret
    toks = c.get("/api/v1/admin/accounts/acctA/tokens", headers=AUTH).json()["tokens"]
    assert len(toks) == 2 and all("token" not in t for t in toks)
    assert c.get("/api/v1/admin/accounts/ghost/tokens", headers=AUTH).status_code == 404
    # revoke
    assert c.post("/api/v1/admin/tokens/%s/revoke" % th, headers=AUTH).json()["revoked"] is True
    assert store.get_token(th)["revoked"] == 1
    assert c.post("/api/v1/admin/tokens/ghosthash/revoke", headers=AUTH).status_code == 404


def test_token_rotate_api(tmp_path):
    app, store = _app(tmp_path, scopes=("accounts",))
    store.add_account("acctA", "A")
    c = TestClient(app)
    th = c.post("/api/v1/admin/accounts/acctA/tokens", headers=AUTH,
                json={"name": "ci", "scopes": ["manage"]}).json()["token_hash"]
    new = c.post("/api/v1/admin/tokens/%s/rotate" % th, headers=AUTH).json()
    assert new["token"] and new["scopes"] == ["manage"] and new["account_id"] == "acctA"
    assert new["token_hash"] != th
    assert store.get_token(th)["revoked"] == 1                  # old revoked
    assert store.get_token(new["token_hash"])["revoked"] == 0   # replacement live
    assert c.post("/api/v1/admin/tokens/ghost/rotate", headers=AUTH).status_code == 404


def test_account_lifecycle_api(tmp_path):
    app, store = _app(tmp_path, scopes=("accounts",))
    store.add_account("acctA", "A")
    c = TestClient(app)
    # rename
    assert c.patch("/api/v1/admin/accounts/acctA", headers=AUTH,
                   json={"name": "Renamed"}).json()["name"] == "Renamed"
    assert store.get_account("acctA")["name"] == "Renamed"
    assert c.patch("/api/v1/admin/accounts/ghost", headers=AUTH, json={"name": "x"}).status_code == 404
    # deactivate -> revokes the account's tokens + flips active; then no mint (issue/rotate -> 409)
    th = c.post("/api/v1/admin/accounts/acctA/tokens", headers=AUTH, json={"name": "ci"}).json()["token_hash"]
    d = c.post("/api/v1/admin/accounts/acctA/deactivate", headers=AUTH).json()
    assert d["active"] is False and d["tokens_revoked"] == 1
    assert store.get_token(th)["revoked"] == 1 and store.get_account("acctA")["active"] == 0
    assert c.post("/api/v1/admin/accounts/acctA/tokens", headers=AUTH,
                  json={"name": "x"}).status_code == 409
    assert c.post("/api/v1/admin/tokens/%s/rotate" % th, headers=AUTH).status_code == 409
    # activate -> minting works again
    assert c.post("/api/v1/admin/accounts/acctA/activate", headers=AUTH).json()["active"] is True
    assert c.post("/api/v1/admin/accounts/acctA/tokens", headers=AUTH,
                  json={"name": "y"}).status_code == 200
    assert c.post("/api/v1/admin/accounts/ghost/deactivate", headers=AUTH).status_code == 404
    assert c.post("/api/v1/admin/accounts/ghost/activate", headers=AUTH).status_code == 404


def test_account_name_validation_api(tmp_path):
    app, store = _app(tmp_path, scopes=("accounts",))
    c = TestClient(app)
    assert c.post("/api/v1/admin/accounts", headers=AUTH, json={"name": "  "}).status_code == 400
    assert c.post("/api/v1/admin/accounts", headers=AUTH, json={"name": "DroneCo"}).status_code == 200
    assert c.post("/api/v1/admin/accounts", headers=AUTH,       # case-insensitive dup
                  json={"name": "droneco"}).status_code == 409
    store.add_account("acctX", "LockCo")
    assert c.patch("/api/v1/admin/accounts/acctX", headers=AUTH, json={"name": " "}).status_code == 400
    assert c.patch("/api/v1/admin/accounts/acctX", headers=AUTH,
                   json={"name": "DroneCo"}).status_code == 409
    assert c.patch("/api/v1/admin/accounts/acctX", headers=AUTH,   # renaming to its own name is fine
                   json={"name": "LockCo"}).status_code == 200


def test_token_management_needs_accounts_scope(tmp_path):
    # a worker token (manage) must NOT mint/list/revoke/rotate -> a stolen worker token is a dead end
    app, store = _app(tmp_path, scopes=("manage", "observe"))
    store.add_account("acctA", "A")
    c = TestClient(app)
    assert c.post("/api/v1/admin/accounts/acctA/tokens", headers=AUTH,
                  json={"name": "x"}).status_code == 403
    assert c.get("/api/v1/admin/accounts/acctA/tokens", headers=AUTH).status_code == 403
    assert c.post("/api/v1/admin/tokens/h/revoke", headers=AUTH).status_code == 403
    assert c.post("/api/v1/admin/tokens/h/rotate", headers=AUTH).status_code == 403


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
                        ["publish", "manage", "observe"], account_id=acc)
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


def test_admin_bind_device_override_and_no_theft(tmp_path):
    app, store = _two_accounts(tmp_path)
    c = TestClient(app)
    # A claims a device currently in B's fleet -> admin binding + the devices row syncs immediately
    store.upsert_device(device_id="d1", product_id=BID, account_id="acctB")
    assert c.post("/api/v1/admin/devices/d1/account", headers=A).json()["account_id"] == "acctA"
    assert store.device_account("d1") == {"account_id": "acctA", "source": "admin"}
    assert store.get_device("d1")["account_id"] == "acctA"         # row synced, not waiting for check-in
    assert c.get("/api/v1/admin/devices", headers=B).json()["devices"] == []   # B no longer sees it
    # A recovers a device wrongly *learned* onto acctB (learned is overridable)
    store.bind_device_account("d2", "acctB", source="learned")
    assert c.post("/api/v1/admin/devices/d2/account", headers=A).status_code == 200
    assert store.device_account("d2")["account_id"] == "acctA"
    # but B cannot STEAL a device A has admin-bound -> 404, and the binding is untouched
    assert c.post("/api/v1/admin/devices/d1/account", headers=B).status_code == 404
    assert store.device_account("d1")["account_id"] == "acctA"
    assert any(e["action"] == "device.bind"
               for e in c.get("/api/v1/admin/audit", headers=A).json()["events"])


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
            return Principal(name="web-user", scopes=["observe"], account_id="acctZ")

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

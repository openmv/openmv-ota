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
    store.set_meta("capability_secret", "x")
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
        "cohorts": [{"cohort": "__default__", "devices": 2,
                     "by_product": {str(BID): 2}, "pins": {}}], "total": 1}
    r = c.post("/api/v1/admin/cohorts/assign", headers=AUTH,
               json={"cohort": "beta", "device_ids": ["d1", "ghost"]})   # ghost doesn't exist
    assert r.json() == {"cohort": "beta", "assigned": 1}                 # only d1 was updated
    got = {x["cohort"]: x["devices"]
           for x in c.get("/api/v1/admin/cohorts", headers=AUTH).json()["cohorts"]}
    assert got == {"__default__": 1, "beta": 1}
    empty = c.post("/api/v1/admin/cohorts/assign", headers=AUTH,
                   json={"cohort": "beta", "device_ids": []})
    assert empty.json() == {"cohort": "beta", "assigned": 0}   # no-op when nothing to assign


def test_cohort_assign_by_product_and_selector_rule(tmp_path):
    """product_id is the bulk selector (every device of the product, same account scoping);
    exactly one of device_ids/product_id must be given -- both or neither is a 400."""
    app, store = _app(tmp_path)
    for d in ("d1", "d2"):
        store.upsert_device(device_id=d, product_id=BID)
    store.upsert_device(device_id="other", product_id=BID + 1)
    c = TestClient(app)
    r = c.post("/api/v1/admin/cohorts/assign", headers=AUTH,
               json={"cohort": "beta", "product_id": BID})
    assert r.json() == {"cohort": "beta", "assigned": 2}
    assert store.get_device("other")["cohort"] == "__default__"
    both = c.post("/api/v1/admin/cohorts/assign", headers=AUTH,
                  json={"cohort": "b", "device_ids": ["d1"], "product_id": BID})
    neither = c.post("/api/v1/admin/cohorts/assign", headers=AUTH, json={"cohort": "b"})
    assert both.status_code == 400 and neither.status_code == 400


def test_cohort_rename_and_delete_validation(tmp_path):
    """The 400s: empty/equal names, __default__ on either side of rename, deleting
    __default__ -- each refused with a reason, nothing touched."""
    app, store = _app(tmp_path)
    c = TestClient(app)
    rn = "/api/v1/admin/cohorts/rename"
    dl = "/api/v1/admin/cohorts/delete"
    assert c.post(rn, headers=AUTH, json={"cohort": "a", "name": "a"}).status_code == 400
    assert c.post(rn, headers=AUTH, json={"cohort": "", "name": "b"}).status_code == 400
    assert c.post(rn, headers=AUTH, json={"cohort": "__default__", "name": "b"}).status_code == 400
    assert c.post(rn, headers=AUTH, json={"cohort": "a", "name": "__default__"}).status_code == 400
    assert c.post(dl, headers=AUTH, json={"cohort": "__default__"}).status_code == 400
    assert c.post(dl, headers=AUTH, json={"cohort": ""}).status_code == 400


def test_cohort_create_declares_an_empty_label(tmp_path):
    """A declared label exists with 0 devices (it shows in list), is refused when the
    name is in use anywhere (device rows, rollouts, pins, or declared) or is __default__,
    follows a rename, and goes away on delete. `assign` auto-declares, so a label that
    sprang into being on a device row is on the books the same way."""
    app, store = _app(tmp_path)
    store.upsert_device(device_id="d1", product_id=BID)
    c = TestClient(app)
    cr = "/api/v1/admin/cohorts/create"
    assert c.post(cr, headers=AUTH, json={"cohort": "staging"}).json() == {"cohort": "staging"}
    rows = {x["cohort"]: x for x in c.get("/api/v1/admin/cohorts", headers=AUTH).json()["cohorts"]}
    assert rows["staging"] == {"cohort": "staging", "devices": 0, "by_product": {},
                               "pins": {}}
    assert c.post(cr, headers=AUTH, json={"cohort": "staging"}).status_code == 409     # declared
    assert c.post(cr, headers=AUTH, json={"cohort": "__default__"}).status_code == 400
    assert c.post(cr, headers=AUTH, json={"cohort": " "}).status_code == 400
    # a label in use only on device rows is just as taken
    c.post("/api/v1/admin/cohorts/assign", headers=AUTH, json={"cohort": "beta", "device_ids": ["d1"]})
    assert c.post(cr, headers=AUTH, json={"cohort": "beta"}).status_code == 409
    # rename carries the declaration; delete drops it
    c.post("/api/v1/admin/cohorts/rename", headers=AUTH, json={"cohort": "staging", "name": "pilot"})
    names = {x["cohort"] for x in c.get("/api/v1/admin/cohorts", headers=AUTH).json()["cohorts"]}
    assert "pilot" in names and "staging" not in names
    c.post("/api/v1/admin/cohorts/delete", headers=AUTH, json={"cohort": "pilot"})
    names = {x["cohort"] for x in c.get("/api/v1/admin/cohorts", headers=AUTH).json()["cohorts"]}
    assert "pilot" not in names
    # the auto-declared 'beta' survives its devices leaving (still listed, at 0)
    c.post("/api/v1/admin/cohorts/assign", headers=AUTH,
           json={"cohort": "__default__", "device_ids": ["d1"]})
    rows = {x["cohort"]: x for x in c.get("/api/v1/admin/cohorts", headers=AUTH).json()["cohorts"]}
    assert rows["beta"]["devices"] == 0
    assert "__default__" not in {r for r in rows if rows[r]["devices"] == 0 and r != "beta"}


def test_cohort_list_shows_pins(tmp_path):
    """A pin is per (product, cohort); the list carries it so a dashboard can show
    'pinned to X' -- and drops it on clear."""
    app, store = _app(tmp_path)
    store.upsert_device(device_id="d1", product_id=BID, cohort="beta")
    _seed_release(store, "rel_p")
    c = TestClient(app)
    c.post("/api/v1/admin/cohorts/pin", headers=AUTH,
           json={"product_id": BID, "cohort": "beta", "release_id": "rel_p"})
    rows = {x["cohort"]: x for x in c.get("/api/v1/admin/cohorts", headers=AUTH).json()["cohorts"]}
    assert rows["beta"]["pins"] == {str(BID): "rel_p"}
    c.post("/api/v1/admin/cohorts/pin", headers=AUTH,
           json={"product_id": BID, "cohort": "beta", "release_id": None})
    rows = {x["cohort"]: x for x in c.get("/api/v1/admin/cohorts", headers=AUTH).json()["cohorts"]}
    assert rows["beta"]["pins"] == {}


def test_cohort_assign_audit_lists_the_devices(tmp_path):
    """The audit row for an assign names the devices (capped at 100 with a
    truncated count), so a history view can say WHAT moved, not just how many."""
    app, store = _app(tmp_path)
    store.upsert_device(device_id="d1", product_id=BID)
    c = TestClient(app)
    c.post("/api/v1/admin/cohorts/assign", headers=AUTH,
           json={"cohort": "beta", "device_ids": ["d1", "ghost"]})
    ev = [e for e in c.get("/api/v1/admin/audit", headers=AUTH).json()["events"]
          if e["action"] == "cohort.assign"][-1]
    assert ev["data"]["device_ids"] == ["d1", "ghost"] and "truncated" not in ev["data"]
    big = ["d%03d" % i for i in range(130)]
    c.post("/api/v1/admin/cohorts/assign", headers=AUTH, json={"cohort": "beta", "device_ids": big})
    ev = [e for e in c.get("/api/v1/admin/audit", headers=AUTH).json()["events"]
          if e["action"] == "cohort.assign"][-1]
    assert len(ev["data"]["device_ids"]) == 100 and ev["data"]["truncated"] == 30


def test_list_contract_sort_page_and_filtered_totals(tmp_path):
    """Every collection endpoint: ?sort/?dir order (whitelisted -- an unknown key falls
    back to natural order, never an error), ?limit/?offset page, and `total` counts
    the FILTERED set. Devices also take ?q (name-or-id substring) and ?cohort_not."""
    app, store = _app(tmp_path)
    for i, (rid, pv) in enumerate((("r1", 0x01000000), ("r2", 0x03000000), ("r3", 0x02000000))):
        _seed_release(store, rid, pv=pv)
    for d, cohort in (("d1", "beta"), ("d2", "__default__"), ("d3", "beta")):
        store.upsert_device(device_id=d, product_id=BID, cohort=cohort)
    store.set_device_name("d2", "Dock east")
    for rid, cohort, state in (("ro_a", "beta", "active"), ("ro_b", "beta", "stopped")):
        store.add_rollout(rollout_id=rid, release_id="r1", product_id=BID, cohort=cohort,
                          percent=10, state=state)
    c = TestClient(app)
    g = lambda path, **q: c.get("/api/v1/admin/" + path, headers=AUTH, params=q).json()  # noqa: E731
    # releases: sort asc/desc by version, paged, total = all
    body = g("releases", sort="version", dir="asc", limit=2)
    assert [r["release_id"] for r in body["releases"]] == ["r1", "r3"] and body["total"] == 3
    assert [r["release_id"] for r in g("releases", sort="version", dir="desc")["releases"]] == ["r2", "r3", "r1"]
    assert g("releases", sort="bogus")["releases"][0]["release_id"] == "r2"   # natural order
    assert g("releases", offset=2, limit=2)["releases"] == g("releases")["releases"][2:]
    # rollouts: total respects state + cohort; up_to_date counts the audience on the
    # release or newer (d1 runs past r1; d3 has reported nothing yet)
    store.upsert_device(device_id="d1", product_id=BID, cohort="beta",
                        current_payload_version=0x02000000)
    row = next(r for r in g("rollouts", cohort="beta", state="active")["rollouts"])
    assert row["cohort_devices"] == 2 and row["up_to_date"] == 1 and row["pause_reason"] is None
    assert g("rollouts", cohort="beta")["total"] == 2
    # why a rollout is paused rides on the row, and filters: an operator's pause, a
    # supersession, and (elsewhere) the failure limit; resuming or stopping clears it
    assert c.patch("/api/v1/admin/rollouts/ro_a", headers=AUTH, json={"state": "paused"}).status_code == 200
    assert g("rollouts", pause_reason="operator")["total"] == 1
    assert g("rollouts", pause_reason="operator")["rollouts"][0]["rollout_id"] == "ro_a"
    assert c.patch("/api/v1/admin/rollouts/ro_a", headers=AUTH, json={"state": "active"}).status_code == 200
    assert g("rollouts", pause_reason="operator")["total"] == 0
    assert c.post("/api/v1/admin/rollouts", headers=AUTH,
                  json={"release_id": "r2", "cohort": "beta", "percent": 5}).status_code == 200
    assert g("rollouts", state="paused", pause_reason="superseded")["rollouts"][0]["rollout_id"] == "ro_a"
    # devices: the attention filters (fell back, unconfirmed, quiet since an instant)
    store.upsert_device(device_id="d2", product_id=BID, fallback_reason="A:body-sha", confirmed=0)
    assert [d["device_id"] for d in g("devices", fell_back="true")["devices"]] == ["d2"]
    assert g("devices", unconfirmed="true")["total"] == 1
    assert g("devices", not_seen_since=2_000_000_000)["total"] == 3          # nobody seen after 2033
    assert g("devices", not_seen_since=0)["total"] == 0                      # everyone since 1970
    assert g("rollouts", cohort="beta", state="stopped")["total"] == 1
    assert g("rollouts", sort="state", dir="asc")["rollouts"][0]["state"] == "active"
    # devices: q, cohort_not, sort by device name (display name counts), filtered total
    assert [d["device_id"] for d in g("devices", q="dock")["devices"]] == ["d2"]
    assert g("devices", q="D")["total"] == 3                                    # case-insensitive
    assert sorted(d["device_id"] for d in g("devices", cohort_not="beta")["devices"]) == ["d2"]
    assert g("devices", cohort="beta")["total"] == 2                            # not the fleet
    names = [d["device_id"] for d in g("devices", sort="device", dir="asc")["devices"]]
    assert names[0] == "d1" and "d2" in names                                    # 'dock east' sorts after 'd1'
    # cohorts: sort by devices, paged, total
    body = g("cohorts", sort="devices", dir="desc", limit=1)
    assert body["cohorts"][0]["cohort"] == "beta" and body["total"] == 2
    # the fleet summary links each version string to its release (newest when republished)
    assert g("fleet")["products"][str(BID)]["releases"] == {
        "2.0.0": {"release_id": "r2", "display_name": ""}}
    # products: the directory on the contract too -- sort by device count, page, total,
    # the newest release's version rides along, and up_to_date counts devices at or
    # past it (d1 runs 0x02000000 = r1... the newest is r2 at 0x03000000: none yet)
    body = g("products", sort="devices", dir="desc", limit=1)
    assert body["total"] == 1 and body["products"][0]["devices"] == 3
    assert body["products"][0]["up_to_date"] == 0
    store.upsert_device(device_id="d3", product_id=BID, cohort="beta", current_payload_version=0x03000000)
    assert g("products", sort="share", dir="desc")["products"][0]["up_to_date"] == 1
    assert body["products"][0]["newest_version"] == g("releases", sort="version", dir="desc")["releases"][0]["version"]
    assert g("products", offset=1)["products"] == []
    # audit: total + offset + sort by action; newest still works. Store-seeded rows
    # write no audit, so two API actions make the entries.
    assert c.post("/api/v1/admin/cohorts/create", headers=AUTH, json={"cohort": "staging"}).status_code == 200
    assert c.patch("/api/v1/admin/rollouts/ro_a", headers=AUTH, json={"percent": 50}).status_code == 200
    a = g("audit")
    assert a["total"] == len(a["events"]) >= 2
    assert g("audit", sort="action", dir="asc")["events"][0]["action"] <= \
        g("audit", sort="action", dir="desc")["events"][0]["action"]
    assert g("audit", offset=1)["events"] == a["events"][1:]
    hidden = g("audit", action_not=a["events"][0]["action"])
    assert hidden["total"] == a["total"] - sum(1 for e in a["events"] if e["action"] == a["events"][0]["action"])
    assert all(e["action"] != a["events"][0]["action"] for e in hidden["events"])
    assert g("audit", newest="true")["events"][0]["seq"] == a["events"][-1]["seq"]
    # devices ?version; rollouts ?release_id (both counted)
    store.upsert_device(device_id="d1", product_id=BID, cohort="beta", current_version="2.0.0")
    assert g("devices", version="2.0.0")["total"] == 1
    assert [d["device_id"] for d in g("devices", version="2.0.0")["devices"]] == ["d1"]
    assert g("rollouts", release_id="r1")["total"] == 2 and g("rollouts", release_id="nope")["total"] == 0
    # devices ?older_than_release: running something older than a release (NULL counts
    # as older; an unknown/foreign release is a 404)
    store.upsert_device(device_id="d2", product_id=BID, cohort="__default__",
                        current_payload_version=0x01000000)         # older than r3 (0x02..)
    store.upsert_device(device_id="d3", product_id=BID, cohort="__default__",
                        current_payload_version=0x03000000)         # newer
    older = g("devices", older_than_release="r3")
    assert older["total"] == 2 and sorted(d["device_id"] for d in older["devices"]) == ["d1", "d2"]
    assert c.get("/api/v1/admin/devices", headers=AUTH,
                 params={"older_than_release": "nope"}).status_code == 404
    # products: the directory
    prods = g("products")
    assert prods["total"] == 1 and prods["products"][0] == {
        "product_id": BID, "product": "P", "display_name": "", "manifest_name": "P",
        "devices": 3, "releases": 3, "up_to_date": 1,                     # d3 reached r2 above
        "newest_version": "2.0.0", "newest_payload_version": 0x03000000}   # the seed stamps every release 2.0.0
    # a display name is the label shown; clearing it brings the manifest name back;
    # an id the account never saw is a 404; every change is audited
    r = c.patch(f"/api/v1/admin/products/{BID}/name", headers=AUTH, json={"name": "Orchard"})
    assert r.status_code == 200 and r.json() == {"product_id": BID, "display_name": "Orchard"}
    row = g("products")["products"][0]
    assert row["product"] == "Orchard" and row["display_name"] == "Orchard" and row["manifest_name"] == "P"
    assert c.patch(f"/api/v1/admin/products/{BID}/name", headers=AUTH, json={"name": ""}).status_code == 200
    assert g("products")["products"][0]["product"] == "P"
    assert c.patch("/api/v1/admin/products/999/name", headers=AUTH, json={"name": "x"}).status_code == 404
    assert [e["action"] for e in store.read_audit()].count("product.rename") == 2


def test_advisories_list_contract(tmp_path):
    app, store = _app(tmp_path)
    _seed_release(store, "r1")
    store.upsert_advisories("r1", [
        {"vuln_id": "CVE-1", "component": "a", "version": "1", "severity": "low", "summary": ""},
        {"vuln_id": "CVE-2", "component": "b", "version": "1", "severity": "critical", "summary": ""},
        {"vuln_id": "CVE-3", "component": "c", "version": "1", "severity": "medium", "summary": ""},
    ], account_id="")
    c = TestClient(app)
    g = lambda **q: c.get("/api/v1/admin/advisories", headers=AUTH, params=q).json()  # noqa: E731
    store.set_release_name("r1", "Night vision")
    rows = g(sort="severity", dir="asc")["advisories"]
    assert [a["vuln_id"] for a in rows] == ["CVE-2", "CVE-3", "CVE-1"]
    assert all(a["release_name"] == "Night vision" for a in rows)            # no second lookup
    body = g(sort="advisory", dir="asc", limit=2)
    assert [a["vuln_id"] for a in body["advisories"]] == ["CVE-1", "CVE-2"] and body["total"] == 3
    assert g(release_id="r1", offset=2, sort="advisory")["advisories"][0]["vuln_id"] == "CVE-3"


def test_rollout_patch_failure_threshold(tmp_path):
    """The limit is changeable mid-rollout (0..1, validated) and audited; changing it
    on a paused rollout does NOT resume it."""
    app, store = _app(tmp_path)
    _seed_release(store, "r1")
    store.add_rollout(rollout_id="ro_a", release_id="r1", product_id=BID, cohort="beta",
                      percent=10, state="paused")
    c = TestClient(app)
    r = c.patch("/api/v1/admin/rollouts/ro_a", headers=AUTH, json={"failure_threshold": 0.2})
    assert r.status_code == 200 and r.json()["failure_threshold"] == 0.2
    assert r.json()["state"] == "paused"                                   # still paused
    assert c.patch("/api/v1/admin/rollouts/ro_a", headers=AUTH,
                   json={"failure_threshold": 1.5}).status_code == 400
    ev = [e for e in c.get("/api/v1/admin/audit", headers=AUTH).json()["events"]
          if e["action"] == "rollout.update"][-1]
    assert ev["data"] == {"failure_threshold": 0.2}


def test_account_device_limit_is_set_by_operator(tmp_path):
    """PUT /accounts/{id}/limit sets the entitlement (null lifts it, negatives are a 400);
    over_device_limit answers 'would one more exceed it'."""
    app, store = _app(tmp_path, scopes=("accounts",))          # operator token
    c = TestClient(app)
    aid = c.post("/api/v1/admin/accounts", headers=AUTH, json={"name": "Lim"}).json()["account_id"]
    for d in ("d1", "d2"):
        store.upsert_device(device_id=d, product_id=BID, account_id=aid)
    r = c.put(f"/api/v1/admin/accounts/{aid}/limit", headers=AUTH, json={"device_limit": 2})
    assert r.json() == {"account_id": aid, "device_limit": 2, "devices": 2}
    assert store.over_device_limit(aid) is True
    assert c.put(f"/api/v1/admin/accounts/{aid}/limit", headers=AUTH,
                 json={"device_limit": -1}).status_code == 400
    assert c.put("/api/v1/admin/accounts/nope/limit", headers=AUTH,
                 json={"device_limit": 1}).status_code == 404
    r = c.put(f"/api/v1/admin/accounts/{aid}/limit", headers=AUTH, json={"device_limit": None})
    assert r.json()["device_limit"] is None and store.over_device_limit(aid) is False
    assert store.over_device_limit("") is False               # no account: never limited
    assert [e for e in store.read_audit(100) if e["action"] == "account.limit"]


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


def test_stop(tmp_path):
    app, store = _app(tmp_path)
    c = TestClient(app)
    rid = _make_rollout(c, store)
    assert c.post("/api/v1/admin/rollouts/%s/stop" % rid,
                  headers=AUTH).json()["state"] == "stopped"
    assert store.get_rollout(rid)["state"] == "stopped"
    assert c.post("/api/v1/admin/rollouts/nope/stop", headers=AUTH).status_code == 404
    # stop is TERMINAL: the docs promise it, so resume must refuse rather than quietly
    # re-offer a release the operator ended -- create a new rollout instead
    r = c.patch("/api/v1/admin/rollouts/%s" % rid, headers=AUTH, json={"state": "active"})
    assert r.status_code == 409 and "stopped" in r.json()["detail"]
    assert store.get_rollout(rid)["state"] == "stopped"


# --- observability --------------------------------------------------------------------------

def test_list_rollouts_cohort_filter(tmp_path):
    """?cohort narrows to the rollouts targeting one label (any product); with
    ?state it separates a cohort's live rollouts from its history."""
    app, store = _app(tmp_path)
    _seed_release(store, "r1")
    for rid, cohort, state in (("ro_a", "beta", "active"), ("ro_b", "beta", "stopped"),
                               ("ro_c", "canary", "active")):
        store.add_rollout(rollout_id=rid, release_id="r1", product_id=BID, cohort=cohort,
                          percent=10, state=state)
    c = TestClient(app)
    def ids(q):
        return sorted(r["rollout_id"] for r in
                      c.get("/api/v1/admin/rollouts", headers=AUTH, params=q).json()["rollouts"])
    assert ids({"cohort": "beta"}) == ["ro_a", "ro_b"]
    assert ids({"cohort": "beta", "state": "active"}) == ["ro_a"]
    assert ids({"cohort": "nope"}) == []


def test_list_rollouts_and_status(tmp_path):
    app, store = _app(tmp_path)
    c = TestClient(app)
    rid = _make_rollout(c, store)
    assert [r["rollout_id"] for r in c.get("/api/v1/admin/rollouts", headers=AUTH).json()
            ["rollouts"]] == [rid]
    st = c.get("/api/v1/admin/rollouts/%s/status" % rid, headers=AUTH).json()
    assert st["rates"] is None                               # nothing staged yet
    store.bump_rollout(rid, attempted=4, updated=3)
    st2 = c.get("/api/v1/admin/rollouts/%s/status" % rid, headers=AUTH).json()
    assert st2["attempted"] == 4 and st2["updated"] == 3
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
    # audited under the token's account (the caller is an operator with no account)
    assert [(e["action"], e["entity_id"]) for e in store.read_audit(account_id="acctA")] \
        == [("token.issue", th)]
    ev = store.read_audit(account_id="acctA")[0]
    assert ev["actor"] == "ci" and "via" not in ev["data"]             # the operator itself
    # on a person's behalf (a web console): actor is the person, via is the operator
    hinted = c.post("/api/v1/admin/accounts/acctA/tokens", headers=AUTH,
                    json={"name": "for-kwabena", "actor": "kwabena"}).json()["token_hash"]
    ev = next(e for e in store.read_audit(account_id="acctA") if e["entity_id"] == hinted)
    assert ev["actor"] == "kwabena" and ev["data"]["via"] == "ci"
    assert c.post("/api/v1/admin/tokens/%s/revoke" % hinted, headers=AUTH,
                  json={"actor": "kwabena"}).json()["revoked"] is True
    ev = [e for e in store.read_audit(account_id="acctA") if e["action"] == "token.revoke"][-1]
    assert ev["actor"] == "kwabena" and ev["data"] == {"name": "for-kwabena", "via": "ci"}
    # explicit scopes, a bad scope, and a missing account
    assert c.post("/api/v1/admin/accounts/acctA/tokens", headers=AUTH,
                  json={"name": "ro", "scopes": ["observe"]}).json()["scopes"] == ["observe"]
    # the ladder: naming a rung stores the rungs below it too
    assert c.post("/api/v1/admin/accounts/acctA/tokens", headers=AUTH,
                  json={"name": "ops", "scopes": ["manage"]}).json()["scopes"] == ["manage", "observe"]
    # names are unique among an account's LIVE tokens (the audit actor); revoking frees one
    dup = c.post("/api/v1/admin/accounts/acctA/tokens", headers=AUTH, json={"name": "ops"})
    assert dup.status_code == 409 and "ops" in dup.json()["detail"]
    store.add_account("acctB", "B")
    assert c.post("/api/v1/admin/accounts/acctB/tokens", headers=AUTH,
                  json={"name": "ops"}).status_code == 200          # other account: fine
    assert c.post("/api/v1/admin/accounts/acctA/tokens", headers=AUTH,
                  json={"name": "x", "scopes": ["god"]}).status_code == 400
    assert c.post("/api/v1/admin/accounts/ghost/tokens", headers=AUTH,
                  json={"name": "x"}).status_code == 404
    # list is metadata only -- never the secret
    toks = c.get("/api/v1/admin/accounts/acctA/tokens", headers=AUTH).json()["tokens"]
    assert len(toks) == 4 and all("token" not in t for t in toks)
    ops = next(x for x in toks if x["name"] == "ops")["token_hash"]
    assert c.post("/api/v1/admin/tokens/%s/revoke" % ops, headers=AUTH).status_code == 200
    assert c.post("/api/v1/admin/accounts/acctA/tokens", headers=AUTH,
                  json={"name": "ops"}).status_code == 200          # name freed by the revoke
    assert c.get("/api/v1/admin/accounts/ghost/tokens", headers=AUTH).status_code == 404
    # revoke
    assert c.post("/api/v1/admin/tokens/%s/revoke" % th, headers=AUTH).json()["revoked"] is True
    assert store.get_token(th)["revoked"] == 1
    assert [(e["action"], e["data"]["name"]) for e in store.read_audit(account_id="acctA")
            if e["action"] == "token.revoke" and e["entity_id"] == th] \
        == [("token.revoke", "ci")]                              # also under the token's account
    assert c.post("/api/v1/admin/tokens/ghosthash/revoke", headers=AUTH).status_code == 404


def test_token_rotate_api(tmp_path):
    app, store = _app(tmp_path, scopes=("accounts",))
    store.add_account("acctA", "A")
    c = TestClient(app)
    th = c.post("/api/v1/admin/accounts/acctA/tokens", headers=AUTH,
                json={"name": "ci", "scopes": ["manage"]}).json()["token_hash"]
    new = c.post("/api/v1/admin/tokens/%s/rotate" % th, headers=AUTH,
                 json={"actor": "kwabena"}).json()
    rot = [e for e in store.read_audit(account_id="acctA") if e["action"] == "token.rotate"][-1]
    assert rot["actor"] == "kwabena" and rot["data"]["via"] == "ci"
    assert new["token"] and new["scopes"] == ["manage", "observe"] and new["account_id"] == "acctA"
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


def test_release_image_is_retained_and_downloadable(tmp_path):
    """Retention is what makes multi-base deltas practical. A delta must be named in the
    SIGNED manifest and the server never holds signing keys, so it can never build one itself
    -- the maker does, locally, and therefore needs the OLD images. Keeping them server-side
    means a build machine does not have to hoard artifacts for every version in the field."""
    app, store = _app(tmp_path)
    storage = app.state.storage
    storage.put("image/rel1", b"OLD-IMAGE-BYTES", "application/gzip")
    store.add_release(release_id="rel1", product_id=BID, product="P", version="1.0.0",
                      payload_version=0x01000000, min_platform_version=0, image_sha256="ab" * 32,
                      image_size=15, representations=[{"format": "full", "url": "x.img.gz",
                                                       "size": 15}],
                      manifest_key="m/rel1", image_key="image/rel1")
    r = TestClient(app).get("/api/v1/admin/releases/rel1/image", headers=AUTH)
    assert r.status_code == 200 and r.content == b"OLD-IMAGE-BYTES"
    assert r.headers["content-type"] == "application/gzip"


def test_release_image_404s_when_not_retained(tmp_path):
    app, store = _app(tmp_path)
    store.add_release(release_id="rel1", product_id=BID, product="P", version="1.0.0",
                      payload_version=0x01000000, min_platform_version=0, image_sha256="ab" * 32,
                      image_size=1, representations=[{"format": "full", "url": "x.img.gz",
                                                      "size": 1}],
                      manifest_key="m/rel1", image_key="image/gone")
    r = TestClient(app).get("/api/v1/admin/releases/rel1/image", headers=AUTH)
    assert r.status_code == 404 and "no longer retained" in r.json()["detail"]
    # ...and an unknown release is a plain 404, indistinguishable from another account's
    assert TestClient(app).get("/api/v1/admin/releases/nope/image",
                               headers=AUTH).status_code == 404


def _seeded_release(app, store, *, release_id="rel1", deltas=()):
    storage = app.state.storage
    storage.put("m/%s" % release_id, b"MANIFEST", "application/octet-stream")
    storage.put("i/%s" % release_id, b"IMAGE", "application/gzip")
    reps = [{"format": "full", "url": "x-ota.img.gz", "size": 5}]
    for name in deltas:
        storage.put("artifacts/%s/%s" % (release_id, name), b"PATCH", "application/gzip")
        reps.append({"format": "ocdl", "url": name, "size": 1, "base_payload_version": 1})
    store.add_release(release_id=release_id, product_id=BID, product="P", version="1.0.0",
                      payload_version=0x01000000, min_platform_version=0,
                      image_sha256="ab" * 32, image_size=5, representations=reps,
                      manifest_key="m/%s" % release_id, image_key="i/%s" % release_id)
    return storage






def test_fleet_devices_audit(tmp_path):
    app, store = _app(tmp_path)
    store.upsert_device(device_id="d1", product_id=BID, board="OPENMV_N6", current_version="1.0.0",
                        slot="A", confirmed=1, fallback_payload_version=0x01000000)
    store.append_audit(actor="ci", action="release.publish", entity_type="release", entity_id="r1")
    c = TestClient(app)
    assert c.get("/api/v1/admin/fleet", headers=AUTH).json()["total"] == 1
    dev = c.get("/api/v1/admin/devices", headers=AUTH).json()["devices"][0]
    assert dev["device_id"] == "d1"
    # the packed number is what the store keeps; a READER gets it decoded, so nobody has to
    # work out that 16777216 means 1.0.0 to answer "what would this device fall back to"
    assert dev["fallback_payload_version"] == 0x01000000 and dev["fallback_version"] == "1.0.0"
    events = c.get("/api/v1/admin/audit", headers=AUTH).json()["events"]
    assert events[0]["action"] == "release.publish"


def test_fleet_breakdowns_and_cohort_filter(tmp_path):
    """The account view structures itself: device counts per product and per cohort ride
    in every summary, and ?cohort= scopes the whole summary to one rollout's audience."""
    app, store = _app(tmp_path)
    store.upsert_device(device_id="a1", product_id=BID, cohort="beta", current_version="1.2.0")
    store.upsert_device(device_id="a2", product_id=BID, cohort="__default__",
                        current_version="1.1.0")
    store.upsert_device(device_id="b1", product_id=BID + 1, cohort="beta",
                        current_version="3.0.0")
    c = TestClient(app)
    body = c.get("/api/v1/admin/fleet", headers=AUTH).json()
    # the dashboard shape: every breakdown nests under its product -- version strings
    # and cohort composition only mean anything within one
    assert body["total"] == 3
    assert body["products"][str(BID)]["by_cohort"] == {"beta": 1, "__default__": 1}
    assert body["products"][str(BID)]["by_version"] == {"1.2.0": 1, "1.1.0": 1}
    assert body["products"][str(BID + 1)] == {
        "total": 1, "by_version": {"3.0.0": 1}, "by_fallback": {"unknown": 1},
        "by_cohort": {"beta": 1}, "releases": {}, "fell_back": 0, "unconfirmed": 0}
    scoped = c.get("/api/v1/admin/fleet?cohort=beta&product_id=%d" % BID, headers=AUTH).json()
    assert scoped["total"] == 1
    assert scoped["products"][str(BID)]["by_version"] == {"1.2.0": 1}
    assert scoped["products"][str(BID)]["by_cohort"] == {"beta": 1}


def test_fleet_summary_reports_exposure_not_slot_names(tmp_path):
    """What an operator watching a rollout needs: who fell back, who is still unproven, and
    what the fleet would fall back TO. A slot NAME answers none of those under A/B."""
    app, store = _app(tmp_path)
    store.upsert_device(device_id="ok", product_id=BID, current_version="1.1.0", slot="B",
                        confirmed=1, fallback_payload_version=0x01000000)
    store.upsert_device(device_id="trial", product_id=BID, current_version="1.1.0", slot="A",
                        confirmed=0, fallback_payload_version=0x01000000)
    store.upsert_device(device_id="back", product_id=BID, current_version="1.0.0", slot="B",
                        confirmed=1, fallback_reason="A:body-sha")
    fs = TestClient(app).get("/api/v1/admin/fleet", headers=AUTH).json()
    assert fs["total"] == 3 and fs["fell_back"] == 1 and fs["unconfirmed"] == 1
    prod = fs["products"][str(BID)]
    # decoded, and a device that did not report its slots reads as "unknown" -- never as 0.0.0
    assert prod["by_fallback"] == {"1.0.0": 2, "unknown": 1}
    assert prod["by_version"] == {"1.1.0": 2, "1.0.0": 1}


# --- account isolation (adversarial: B must never see or touch A's data) --------------------

def _two_accounts(tmp_path):
    store = SqliteMetadataStore(str(tmp_path / "ota.db"))
    store.migrate()
    store.set_meta("capability_secret", "x")
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
    store.set_meta("capability_secret", "x")
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
    assert c.post("/api/v1/admin/rollouts/%s/stop" % roA, headers=B).status_code == 404

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


# --- viewer grants: the dashboard's issuer ---------------------------------------------------

def _live_app(tmp_path, scopes=("manage", "observe")):
    store = SqliteMetadataStore(str(tmp_path / "ota.db"))
    store.migrate()
    store.set_meta("capability_secret", "x")
    store.add_token(hash_token("admintok"), "ci", list(scopes))
    app = create_app(ServerSettings(base_url="https://ota.test", swd_ids_verify_url="u",
                                    swd_ids_verify_token="t",
                                    live_relay_url="https://live.test",
                                    live_token_secret="s3cret",
                                    datalake_url="https://data.test",
                                    datalake_token_secret="dl-s3cret"),
                     metastore=store, storage=LocalArtifactStorage(str(tmp_path / "blobs")),
                     verifier=_Verifier())
    return app, store


def _seed_device(store, device_id="dev1", account_id="", streams="0,thermal"):
    store.upsert_device(device_id=device_id, product_id=BID, board="B",
                        account_id=account_id, streams=streams.split(","))
    return device_id


def test_product_viewer_grant_opens_a_products_data(tmp_path):
    """One credential for a product's data across all its devices: the datalake's
    product token and its two URLs. Only the account's own products; a server without
    a datalake says so."""
    app, store = _live_app(tmp_path)
    _seed_device(store)
    c = TestClient(app)
    r = c.post("/api/v1/admin/products/%d/viewer-grant" % BID, headers=AUTH)
    assert r.status_code == 200
    dl = r.json()["datalake"]
    assert dl["topics_url"] == "https://data.test/api/v1/products/default/%d/topics" % BID
    assert dl["series_url"].endswith("/products/default/%d/series" % BID)
    assert dl["token"] and dl["expires_in_s"] == r.json()["expires_in_s"] > 0
    assert c.post("/api/v1/admin/products/999/viewer-grant", headers=AUTH).status_code == 404
    app2, store2 = _app(_mk(tmp_path, "plain"))
    _seed_device(store2)
    assert TestClient(app2).post("/api/v1/admin/products/%d/viewer-grant" % BID,
                                 headers=AUTH).status_code == 503


def _mk(tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    return d


def test_viewer_grant_returns_watch_and_read_urls(tmp_path):
    app, store = _live_app(tmp_path)
    _seed_device(store)
    store.bind_device_account("dev1", "", source="learned")
    r = TestClient(app).post("/api/v1/admin/devices/dev1/viewer-grant", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert set(body["streams"]) == {"0", "thermal"}      # persisted at check-in
    assert body["streams"]["0"]["watch_url"].startswith("wss://live.test/watch/dev1/0?token=")
    assert body["datalake"]["topics_url"] == "https://data.test/api/v1/topics/dev1"
    assert body["token"] and body["datalake"]["token"] != body["token"]


def test_viewer_grant_needs_the_observe_scope(tmp_path):
    app, store = _live_app(tmp_path, scopes=("accounts",))     # off the ladder: no observe
    _seed_device(store)
    r = TestClient(app).post("/api/v1/admin/devices/dev1/viewer-grant", headers=AUTH)
    assert r.status_code == 403


def test_viewer_grant_hides_another_accounts_device_as_a_404(tmp_path):
    # a device that exists but isn't ours must be indistinguishable from one that
    # doesn't -- otherwise this endpoint enumerates the fleet
    app, store = _live_app(tmp_path)
    _seed_device(store, account_id="acct_other")
    store.bind_device_account("dev1", "acct_other", source="admin")
    r = TestClient(app).post("/api/v1/admin/devices/dev1/viewer-grant", headers=AUTH)
    assert r.status_code == 404
    missing = TestClient(app).post("/api/v1/admin/devices/nope/viewer-grant", headers=AUTH)
    assert missing.status_code == 404


def test_viewer_grant_uses_the_sticky_binding_not_the_reported_account(tmp_path):
    # a device reporting someone else's account (or a golden's blank one) must not
    # move ownership -- the binding is authoritative
    app, store = _live_app(tmp_path)
    _seed_device(store, account_id="acct_other")
    store.bind_device_account("dev1", "", source="admin")     # really ours
    r = TestClient(app).post("/api/v1/admin/devices/dev1/viewer-grant", headers=AUTH)
    assert r.status_code == 200


def test_viewer_grant_503s_when_live_is_not_configured(tmp_path):
    app, store = _app(tmp_path)                              # no relay configured
    _seed_device(store)
    store.bind_device_account("dev1", "", source="learned")
    r = TestClient(app).post("/api/v1/admin/devices/dev1/viewer-grant", headers=AUTH)
    assert r.status_code == 503


# --- single-resource reads (a UI's detail pages) ------------------------------------------------
# A UI can render a list from GET /devices and GET /releases, but a detail page had nothing to ask:
# it would have to page the list until the row appeared, which on a fleet with real history is a
# lot of requests to answer "show me this device".

def test_device_detail_matches_the_list_row(tmp_path):
    """Same shape as a list row -- including the decoded fallback_version -- so a UI can drive a
    list item and a detail page off ONE model instead of two that drift."""
    app, store = _app(tmp_path)
    store.upsert_device(device_id="d1", product_id=BID, board="OPENMV_N6", current_version="1.0.0",
                        slot="A", confirmed=1, fallback_payload_version=0x01000000)
    c = TestClient(app)
    one = c.get("/api/v1/admin/devices/d1", headers=AUTH).json()
    listed = c.get("/api/v1/admin/devices", headers=AUTH).json()["devices"][0]
    assert one == listed
    assert one["fallback_version"] == "1.0.0"


def test_release_detail_returns_the_release(tmp_path):
    app, store = _app(tmp_path)
    _seed_release(store)
    r = TestClient(app).get("/api/v1/admin/releases/rel1", headers=AUTH).json()
    assert r["release_id"] == "rel1" and r["version"] == "2.0.0"
    assert r["representations"][0]["format"] == "full"   # parsed, not the stored JSON string


def test_detail_reads_need_observe_scope(tmp_path):
    app, store = _app(tmp_path, scopes=("accounts",))          # off the ladder: no observe
    store.upsert_device(device_id="d1", product_id=BID)
    _seed_release(store)
    c = TestClient(app)
    assert c.get("/api/v1/admin/devices/d1", headers=AUTH).status_code == 403
    assert c.get("/api/v1/admin/releases/rel1", headers=AUTH).status_code == 403


def test_detail_reads_hide_other_accounts_behind_404(tmp_path):
    """A missing row and another account's row must be indistinguishable, or the endpoint becomes
    a probe for which device ids exist."""
    app, store = _app(tmp_path)
    store.upsert_device(device_id="d1", product_id=BID)
    # what an admin rebind does: the authoritative binding AND the fleet-view column
    store.bind_device_account("d1", "someone-else", source="admin")
    store.set_device_account("d1", "someone-else")
    _seed_release(store, rid="theirs")
    store.execute("UPDATE releases SET account_id = ? WHERE release_id = ?",
                  ("someone-else", "theirs"))
    c = TestClient(app)
    assert c.get("/api/v1/admin/devices/d1", headers=AUTH).status_code == 404
    assert c.get("/api/v1/admin/releases/theirs", headers=AUTH).status_code == 404
    # ...and indistinguishable from a row that simply is not there
    assert c.get("/api/v1/admin/devices/nope", headers=AUTH).status_code == 404
    assert c.get("/api/v1/admin/releases/nope", headers=AUTH).status_code == 404


# --- pagination: uniform, bounded, and never silently truncating -------------------------------

def test_list_endpoints_are_bounded_by_default_and_report_the_total(tmp_path):
    """`/releases` and `/rollouts` used to default to NO limit while `/devices` capped at 100.

    Two problems in one: a caller had to remember which collection happened to be unbounded, and
    a fleet with thousands of releases returned all of them in a single response. Now every
    paginated list defaults to the same page size AND carries `total`, so a full page can be told
    apart from a truncated list -- without it, `limit=100` on 400 releases looks exactly like a
    fleet that has 100.
    """
    app, store = _app(tmp_path)
    for i in range(7):
        _seed_release(store, rid="rel%d" % i, pv=0x02000000 + i)
    c = TestClient(app)
    body = c.get("/api/v1/admin/releases?limit=3", headers=AUTH).json()
    assert len(body["releases"]) == 3, "the page is honoured"
    assert body["total"] == 7, "...and the caller can see what it is a page OF"
    # the default is a bound, not unlimited
    import inspect

    from openmv_ota.server import admin
    assert inspect.signature(admin.releases).parameters["limit"].default == admin._PAGE
    assert inspect.signature(admin.list_rollouts).parameters["limit"].default == admin._PAGE
    assert inspect.signature(admin.devices).parameters["limit"].default == admin._PAGE


def test_total_is_account_scoped_like_the_rows_it_counts(tmp_path):
    """A `total` that ignored the account scope would leak the size of other tenants' fleets."""
    app, store = _app(tmp_path)
    _seed_release(store, rid="mine")
    _seed_release(store, rid="theirs")
    store.execute("UPDATE releases SET account_id = ? WHERE release_id = ?", ("other", "theirs"))
    body = TestClient(app).get("/api/v1/admin/releases", headers=AUTH).json()
    assert [r["release_id"] for r in body["releases"]] == ["mine"]
    assert body["total"] == 1, "counts what this caller may see, not the table"


def test_fleet_bases_names_the_bytes_a_release_must_cover(tmp_path):
    """The release-planning read behind `build ota-romfs --delta-fleet`: distinct
    (version, body_sha256) with device counts, versions decoded for the reader."""
    app, store = _app(tmp_path)
    store.upsert_device(device_id="d1", product_id=BID, current_payload_version=0x01000000,
                        body_sha256="aa" * 32)
    store.upsert_device(device_id="d2", product_id=BID, current_payload_version=0x01000000,
                        body_sha256="aa" * 32)
    store.upsert_device(device_id="d3", product_id=BID, current_payload_version=0x01000100)
    c = TestClient(app)
    bases = c.get("/api/v1/admin/fleet/bases", headers=AUTH,
                  params={"product_id": BID}).json()["bases"]
    assert bases[0] == {"payload_version": 0x01000000, "version": "1.0.0",
                        "body_sha256": "aa" * 32, "devices": 2}
    # the sha-less device is its own row: it can only take full images, and a reader
    # deserves to see that population rather than have it folded into a sha group
    assert {"payload_version": 0x01000100, "version": "1.0.1",
            "body_sha256": "", "devices": 1} in bases


def test_release_sbom_served_and_404s(tmp_path):
    app, store = _app(tmp_path)
    storage = app.state.storage
    store.add_release(release_id="rs", product_id=BID, product="P", version="1.0.0",
                      payload_version=1, min_platform_version=0, image_sha256="ab" * 32,
                      image_size=1, representations=[], manifest_key="m", image_key="i",
                      sbom_key="sbom/rs/sbom.cdx.json")
    storage.put("sbom/rs/sbom.cdx.json", b'{"bomFormat": "CycloneDX"}', "application/json")
    store.add_release(release_id="nosbom", product_id=BID, product="P", version="1.0.1",
                      payload_version=2, min_platform_version=0, image_sha256="ab" * 32,
                      image_size=1, representations=[], manifest_key="m", image_key="i")
    store.add_release(release_id="gone", product_id=BID, product="P", version="1.0.2",
                      payload_version=3, min_platform_version=0, image_sha256="ab" * 32,
                      image_size=1, representations=[], manifest_key="m", image_key="i",
                      sbom_key="sbom/gone/sbom.cdx.json")   # row survives its bytes
    c = TestClient(app)
    r = c.get("/api/v1/admin/releases/rs/sbom", headers=AUTH)
    assert r.status_code == 200 and r.json()["bomFormat"] == "CycloneDX"
    assert c.get("/api/v1/admin/releases/nosbom/sbom", headers=AUTH).status_code == 404
    assert c.get("/api/v1/admin/releases/gone/sbom", headers=AUTH).status_code == 404

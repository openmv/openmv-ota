"""A token limited to some of an account's products.

The account boundary keeps tenants apart. This is the boundary INSIDE an account: a
platform that models each of its own customers as a product hands out one credential per
customer, and none of them may see the rest of the fleet. A half-enforced boundary is
worse than none -- the holder believes it -- so this walks every read and write that
takes a product, not a representative sample.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from openmv_ota.server.app import create_app
from openmv_ota.server.auth import hash_token
from openmv_ota.server.metastore import SqliteMetadataStore
from openmv_ota.server.settings import ServerSettings
from openmv_ota.server.storage import LocalArtifactStorage
from openmv_ota.server.verify import Registration

MINE, THEIRS = 1001, 2002
ACCOUNT = "acct"


class _Verifier:
    def verify(self, board, device_id):
        return Registration(True)


def _app(tmp_path):
    store = SqliteMetadataStore(str(tmp_path / "ota.db"))
    store.migrate()
    store.set_meta("capability_secret", "x")
    # one credential for the whole account, one limited to a single product
    store.add_token(hash_token("whole"), "ci", ["manage", "observe"], account_id=ACCOUNT)
    store.add_token(hash_token("limited"), "partner", ["manage", "observe"],
                    account_id=ACCOUNT, products=[MINE])
    for pid, rid in ((MINE, "rel_mine"), (THEIRS, "rel_theirs")):
        store.add_release(release_id=rid, product_id=pid, product="p%d" % pid, version="1.0.0",
                          payload_version=pid, min_platform_version=0, image_sha256="x",
                          image_size=1, representations=[], manifest_key="m", image_key="i",
                          account_id=ACCOUNT)
        store.upsert_device(device_id="dev_%d" % pid, product_id=pid, board="OPENMV_N6",
                            account_id=ACCOUNT)
    app = create_app(ServerSettings(base_url="https://ota.test", swd_ids_verify_url="u",
                                    swd_ids_verify_token="t"),
                     metastore=store, storage=LocalArtifactStorage(str(tmp_path / "blobs")),
                     verifier=_Verifier())
    return TestClient(app), store


WHOLE = {"Authorization": "Bearer whole"}
LIMITED = {"Authorization": "Bearer limited"}


def test_every_collection_is_filtered_to_the_allowed_products(tmp_path):
    c, _ = _app(tmp_path)
    for path, key, field in (("/api/v1/admin/releases", "releases", "product_id"),
                             ("/api/v1/admin/devices", "devices", "product_id"),
                             ("/api/v1/admin/products", "products", "product_id")):
        whole = c.get(path, headers=WHOLE).json()
        limited = c.get(path, headers=LIMITED).json()
        assert {r[field] for r in whole[key]} == {MINE, THEIRS}, path
        assert {r[field] for r in limited[key]} == {MINE}, path
        # the count must agree with the rows, or paging walks off the end of a lie
        assert limited["total"] == 1, path
        assert whole["total"] == 2, path


def test_the_fleet_views_are_filtered_too(tmp_path):
    c, _ = _app(tmp_path)
    assert set(c.get("/api/v1/admin/fleet", headers=WHOLE).json()["products"]) == {
        str(MINE), str(THEIRS)}
    assert set(c.get("/api/v1/admin/fleet", headers=LIMITED).json()["products"]) == {str(MINE)}
    # cohort device counts are a fleet view by another name
    counts = {row["cohort"]: row["devices"]
              for row in c.get("/api/v1/admin/cohorts", headers=LIMITED).json()["cohorts"]}
    assert counts["__default__"] == 1


def test_another_products_entities_are_404_not_403(tmp_path):
    """404, never 403: a limited credential must not be able to use the error to learn
    which product ids exist, any more than it could across accounts."""
    c, _ = _app(tmp_path)
    for path in ("/api/v1/admin/releases/rel_theirs",
                 "/api/v1/admin/devices/dev_%d" % THEIRS):
        assert c.get(path, headers=WHOLE).status_code == 200, path
        assert c.get(path, headers=LIMITED).status_code == 404, path
    # ...and its own remain reachable
    assert c.get("/api/v1/admin/releases/rel_mine", headers=LIMITED).status_code == 200


def test_writes_into_another_product_are_refused(tmp_path):
    c, _ = _app(tmp_path)
    assert c.patch("/api/v1/admin/products/%d/name" % THEIRS, json={"name": "nope"},
                   headers=LIMITED).status_code == 404
    assert c.patch("/api/v1/admin/products/%d/name" % MINE, json={"name": "fine"},
                   headers=LIMITED).status_code == 200
    assert c.post("/api/v1/admin/cohorts/assign",
                  json={"cohort": "beta", "product_id": THEIRS},
                  headers=LIMITED).status_code == 404
    assert c.post("/api/v1/admin/cohorts/pin",
                  json={"product_id": THEIRS, "cohort": "beta", "release_id": "rel_theirs"},
                  headers=LIMITED).status_code == 404
    # a rollout reaches its product through the release, which _owned already gates
    assert c.post("/api/v1/admin/rollouts",
                  json={"release_id": "rel_theirs", "cohort": "beta", "percent": 5},
                  headers=LIMITED).status_code == 404


def test_the_audit_log_is_filtered_to_the_allowed_products(tmp_path):
    """A limited token reads its own products' history and nothing else.

    An audit row now records the product it happened to, so there IS something to filter
    by. What a limited credential must never see is the rest of the account: the other
    products' activity, and the account-level rows (tokens, billing) that belong to
    whoever owns the account rather than to its customer."""
    c, store = _app(tmp_path)
    store.append_audit(actor="ci", action="device.pin", entity_type="device",
                       entity_id="dev_%d" % MINE, account_id=ACCOUNT, product_id=MINE)
    store.append_audit(actor="ci", action="device.pin", entity_type="device",
                       entity_id="dev_%d" % THEIRS, account_id=ACCOUNT, product_id=THEIRS)
    store.append_audit(actor="ci", action="token.issue", entity_type="token",
                       entity_id="h", account_id=ACCOUNT)          # no product: account-level

    whole = c.get("/api/v1/admin/audit", headers=WHOLE).json()
    assert {e["action"] for e in whole["events"]} == {"device.pin", "token.issue"}
    assert whole["total"] == 3

    mine = c.get("/api/v1/admin/audit", headers=LIMITED).json()
    assert mine["total"] == 1 and len(mine["events"]) == 1
    assert mine["events"][0]["entity_id"] == "dev_%d" % MINE


def test_a_token_scoped_to_nothing_sees_nothing(tmp_path):
    """The empty allow-list is the case a WHERE-builder gets wrong: `IN ()` is a syntax
    error, and skipping the clause turns "allowed nothing" into "allowed everything"."""
    c, store = _app(tmp_path)
    store.add_token(hash_token("empty"), "nothing", ["observe"], account_id=ACCOUNT,
                    products=[999999])
    head = {"Authorization": "Bearer empty"}
    body = c.get("/api/v1/admin/releases", headers=head).json()
    assert body["releases"] == [] and body["total"] == 0
    assert c.get("/api/v1/admin/devices", headers=head).json()["total"] == 0


def test_fleet_bases_is_filtered(tmp_path):
    """The delta-planning read is a fleet view too: it says which builds are running,
    which is exactly what a limited credential must not learn about other products."""
    c, store = _app(tmp_path)
    for pid in (MINE, THEIRS):
        store.upsert_device(device_id="dev_%d" % pid, product_id=pid, board="OPENMV_N6",
                            account_id=ACCOUNT, current_payload_version=pid,
                            body_sha256="%064x" % pid)
    whole = c.get("/api/v1/admin/fleet/bases", headers=WHOLE).json()["bases"]
    limited = c.get("/api/v1/admin/fleet/bases", headers=LIMITED).json()["bases"]
    assert len(whole) == 2 and len(limited) == 1
    assert limited[0]["body_sha256"] == "%064x" % MINE


def test_an_empty_allow_list_is_not_a_wildcard(tmp_path):
    """The store-level contract behind the credential: `products=[]` means NOTHING. It is
    written out because `IN ()` is a syntax error, and the obvious workaround -- drop the
    clause -- would turn a token allowed nothing into one allowed everything."""
    _, store = _app(tmp_path)
    assert store.count_releases(account_id=ACCOUNT) == 2
    assert store.count_releases(account_id=ACCOUNT, products=[]) == 0
    assert store.list_devices(account_id=ACCOUNT, products=[]) == []
    assert store.fleet_bases(account_id=ACCOUNT, products=[]) == []
    store.append_audit(actor="ci", action="device.pin", entity_type="device",
                       entity_id="dev", account_id=ACCOUNT, product_id=MINE)
    assert store.read_audit(account_id=ACCOUNT, products=[]) == []
    assert store.count_audit(account_id=ACCOUNT, products=[]) == 0
    assert store.count_audit(account_id=ACCOUNT, products=[MINE]) == 1


def test_a_limited_token_cannot_also_be_an_operator(tmp_path):
    """`accounts` acts across accounts, where a product list means nothing. Issuing both
    would hand out a credential whose limit is not a limit."""
    c, store = _app(tmp_path)
    store.add_account("other", name="Other", created_by="operator")
    store.add_token(hash_token("op"), "operator", ["accounts"], account_id="")
    op = {"Authorization": "Bearer op"}
    resp = c.post("/api/v1/admin/accounts/other/tokens",
                  json={"name": "partner", "scopes": ["observe", "accounts"],
                        "products": [MINE]}, headers=op)
    assert resp.status_code == 400
    assert "accounts scope" in resp.json()["detail"]
    # ...and the same request without the operator scope is fine
    ok = c.post("/api/v1/admin/accounts/other/tokens",
                json={"name": "partner", "scopes": ["observe"], "products": [MINE]},
                headers=op)
    assert ok.status_code == 200 and ok.json()["products"] == [MINE]


def test_every_product_id_is_also_a_string(tmp_path):
    """JSON numbers are IEEE doubles in JavaScript, so a 63-bit product id is rounded by
    JSON.parse -- silently, and every lookup with the rounded value misses. Python and
    MicroPython are exact, so nothing in this stack would ever notice; an integrator's
    Node service would. Each response carrying the number carries the exact string too.
    """
    c, store = _app(tmp_path)
    big = (1 << 62) + 12345                       # far above 2**53
    store.add_release(release_id="rel_big", product_id=big, product="huge", version="1.0.0",
                      payload_version=9, min_platform_version=0, image_sha256="x", image_size=1,
                      representations=[], manifest_key="m", image_key="i", account_id=ACCOUNT)
    store.upsert_device(device_id="dev_big", product_id=big, board="OPENMV_N6",
                        account_id=ACCOUNT)

    for path, key in (("/api/v1/admin/releases", "releases"),
                      ("/api/v1/admin/devices", "devices"),
                      ("/api/v1/admin/products", "products")):
        row = next(r for r in c.get(path, headers=WHOLE).json()[key]
                   if r["product_id"] == big)
        assert row["product_id_str"] == str(big), path
        # the string survives what the number does not
        assert int(float(row["product_id"])) != big or big < (1 << 53)

    renamed = c.patch("/api/v1/admin/products/%d/name" % big, json={"name": "Huge"},
                      headers=WHOLE).json()
    assert renamed["product_id_str"] == str(big)

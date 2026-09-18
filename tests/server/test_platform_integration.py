"""A platform reselling this server to its own customers.

One server, more than one operator: ours, and a platform that provisions accounts for
its customers through the API and never logs in. Everything here is about the boundary
between those operators, and about the two verbs a platform needs that a single-tenant
operator never notices are missing -- declaring a product before there is anything to
publish to it, and ending an install.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from openmv_ota.server.app import create_app
from openmv_ota.server.auth import hash_token
from openmv_ota.server.metastore import SqliteMetadataStore
from openmv_ota.server.scopes import expand
from openmv_ota.server.settings import ServerSettings
from openmv_ota.server.storage import LocalArtifactStorage
from openmv_ota.server.verify import Registration


class _Verifier:
    def verify(self, board, device_id):
        return Registration(True)


def _app(tmp_path):
    store = SqliteMetadataStore(str(tmp_path / "ota.db"))
    store.migrate()
    store.set_meta("capability_secret", "x")
    # the server's own root, and two platforms that resell it
    store.add_token(hash_token("root"), "openmv", ["accounts.all"])
    store.add_token(hash_token("rf"), "acme-platform", ["accounts"])
    store.add_token(hash_token("other"), "someone-else", ["accounts"])
    app = create_app(ServerSettings(base_url="https://ota.test", swd_ids_verify_url="u",
                                    swd_ids_verify_token="t"),
                     metastore=store, storage=LocalArtifactStorage(str(tmp_path / "blobs")),
                     verifier=_Verifier())
    return TestClient(app), store


ROOT = {"Authorization": "Bearer root"}
RF = {"Authorization": "Bearer rf"}
OTHER = {"Authorization": "Bearer other"}


def test_accounts_all_implies_accounts():
    """Seeing every account is `accounts` without the filter, not a different job -- a
    token issued with only the wider scope must still be able to provision."""
    assert "accounts" in expand(["accounts.all"])
    assert "accounts.all" not in expand(["accounts"])


def test_an_operator_sees_only_the_accounts_it_provisioned(tmp_path):
    """The whole point of the split. Handing a partner the ability to create customers
    used to hand them the customer list of everyone else on the server."""
    c, _ = _app(tmp_path)
    mine = c.post("/api/v1/admin/accounts", headers=RF, json={"name": "Acme"}).json()
    theirs = c.post("/api/v1/admin/accounts", headers=OTHER, json={"name": "Globex"}).json()

    assert [a["account_id"] for a in c.get("/api/v1/admin/accounts", headers=RF)
            .json()["accounts"]] == [mine["account_id"]]
    assert [a["account_id"] for a in c.get("/api/v1/admin/accounts", headers=OTHER)
            .json()["accounts"]] == [theirs["account_id"]]
    # the server's own root sees both, which is what `accounts.all` is for
    assert {a["account_id"] for a in c.get("/api/v1/admin/accounts", headers=ROOT)
            .json()["accounts"]} == {mine["account_id"], theirs["account_id"]}


def test_another_operators_account_is_not_even_there(tmp_path):
    """404 on every verb, not 403: the shape of the refusal must not confirm that an
    account exists. Knowing the id is not supposed to be enough."""
    c, _ = _app(tmp_path)
    theirs = c.post("/api/v1/admin/accounts", headers=OTHER,
                    json={"name": "Globex"}).json()["account_id"]
    aid = "/api/v1/admin/accounts/" + theirs
    assert c.patch(aid, headers=RF, json={"name": "mine now"}).status_code == 404
    assert c.put(aid + "/limit", headers=RF, json={"device_limit": 5}).status_code == 404
    assert c.post(aid + "/deactivate", headers=RF).status_code == 404
    assert c.post(aid + "/activate", headers=RF).status_code == 404
    assert c.post(aid + "/tokens", headers=RF, json={"name": "x"}).status_code == 404
    assert c.get(aid + "/tokens", headers=RF).status_code == 404
    # ...and the root operator can do all of it
    assert c.patch(aid, headers=ROOT, json={"name": "Globex Inc"}).status_code == 200
    assert c.get(aid + "/tokens", headers=ROOT).status_code == 200


def test_one_operator_cannot_revoke_or_rotate_anothers_token(tmp_path):
    """A token hash is not a secret -- it is printed by `tokens` listings and lands in
    audit rows. It must not be a lever on another operator's credential."""
    c, _ = _app(tmp_path)
    theirs = c.post("/api/v1/admin/accounts", headers=OTHER,
                    json={"name": "Globex"}).json()["account_id"]
    th = c.post("/api/v1/admin/accounts/%s/tokens" % theirs, headers=OTHER,
                json={"name": "ci"}).json()["token_hash"]
    assert c.post("/api/v1/admin/tokens/%s/revoke" % th, headers=RF).status_code == 404
    assert c.post("/api/v1/admin/tokens/%s/rotate" % th, headers=RF).status_code == 404
    assert c.post("/api/v1/admin/tokens/%s/revoke" % th, headers=OTHER).status_code == 200


def test_two_platforms_can_both_have_a_customer_called_acme(tmp_path):
    """Server-wide name uniqueness was one namespace for everybody: it blocked a name a
    platform is entitled to use, and the 409 saying so was a way to ask whether a rival
    had a customer by that name."""
    c, _ = _app(tmp_path)
    assert c.post("/api/v1/admin/accounts", headers=RF, json={"name": "Acme"}).status_code == 200
    assert c.post("/api/v1/admin/accounts", headers=OTHER,
                  json={"name": "Acme"}).status_code == 200
    # within one operator it is still unique, case-insensitively
    assert c.post("/api/v1/admin/accounts", headers=RF, json={"name": "acme"}).status_code == 409


def test_create_is_idempotent_under_a_client_ref(tmp_path):
    """A retry after a timeout must not make a second account -- and must not answer with
    a 409 the caller cannot tell apart from someone else owning the name."""
    c, _ = _app(tmp_path)
    first = c.post("/api/v1/admin/accounts", headers=RF,
                   json={"name": "Acme", "client_ref": "ws_42"}).json()
    assert first["created"] is True and first["token"]

    again = c.post("/api/v1/admin/accounts", headers=RF,
                   json={"name": "Acme", "client_ref": "ws_42"}).json()
    assert again["created"] is False
    assert again["account_id"] == first["account_id"]
    # no second token: it was handed over once, and minting another here would leave a
    # live credential nobody is tracking
    assert again["token"] is None

    # the reference belongs to the operator that used it, not to the server
    mine_too = c.post("/api/v1/admin/accounts", headers=OTHER,
                      json={"name": "Acme", "client_ref": "ws_42"}).json()
    assert mine_too["created"] is True and mine_too["account_id"] != first["account_id"]


def test_a_product_can_exist_before_anything_is_published_to_it(tmp_path):
    """The order a platform works in: make the project, name it, bind its first cameras,
    and only then build an image for them."""
    c, store = _app(tmp_path)
    store.add_token(hash_token("acct"), "ci", ["manage", "observe"], account_id="a1")
    auth = {"Authorization": "Bearer acct"}

    made = c.post("/api/v1/admin/products", headers=auth,
                  json={"product_id": 4242, "display_name": "Workflow runner"}).json()
    assert made["created"] is True and made["product_id_str"] == "4242"

    listed = c.get("/api/v1/admin/products", headers=auth).json()["products"]
    assert [(p["product_id"], p["releases"], p["devices"]) for p in listed] == [(4242, 0, 0)]
    assert listed[0]["product"] == "Workflow runner"

    # declaring it again is not an error, and can still set the name
    again = c.post("/api/v1/admin/products", headers=auth,
                   json={"product_id": 4242, "display_name": "Renamed"}).json()
    assert again["created"] is False and again["display_name"] == "Renamed"
    assert c.get("/api/v1/admin/products", headers=auth).json()["products"][0]["product"] \
        == "Renamed"
    # and the declaration is in the account's history, under the product
    hist = c.get("/api/v1/admin/audit", headers=auth).json()["events"]
    assert [e["action"] for e in hist] == ["product.create"]


def test_a_limited_token_cannot_declare_a_product_outside_its_list(tmp_path):
    c, store = _app(tmp_path)
    store.add_token(hash_token("lim"), "partner", ["manage"], account_id="a1", products=[1001])
    auth = {"Authorization": "Bearer lim"}
    assert c.post("/api/v1/admin/products", headers=auth,
                  json={"product_id": 1001}).status_code == 200
    assert c.post("/api/v1/admin/products", headers=auth,
                  json={"product_id": 2002}).status_code == 404


def test_forgetting_a_device_ends_the_install_but_not_the_history(tmp_path):
    """The other half of binding one. Without it a decommissioned camera sits in the
    fleet for good and keeps consuming the account's device limit."""
    c, store = _app(tmp_path)
    store.add_token(hash_token("acct"), "ci", ["manage", "observe"], account_id="a1")
    auth = {"Authorization": "Bearer acct"}
    store.upsert_device(device_id="OPENMV_N6:aa", product_id=7, board="OPENMV_N6",
                        account_id="a1")
    store.bind_device_account("OPENMV_N6:aa", "a1", source="admin")
    store.record_deployment(device_id="OPENMV_N6:aa", release_id="rel_1", product_id=7,
                            status="installed", account_id="a1")

    assert c.delete("/api/v1/admin/devices/OPENMV_N6:aa", headers=auth).json()["forgotten"] is True
    assert store.get_device("OPENMV_N6:aa") is None
    assert store.device_account("OPENMV_N6:aa") is None
    assert store.device_count("a1") == 0                      # the limit is freed
    # what it installed on a day that has passed is not restated
    assert store.query_all("SELECT status FROM deployments")[0]["status"] == "installed"
    # and the removal is itself in the log
    assert [e["action"] for e in c.get("/api/v1/admin/audit", headers=auth).json()["events"]] \
        == ["device.forget"]
    assert c.delete("/api/v1/admin/devices/OPENMV_N6:aa", headers=auth).status_code == 404


def test_a_device_of_another_account_or_product_cannot_be_forgotten(tmp_path):
    c, store = _app(tmp_path)
    store.add_token(hash_token("acct"), "ci", ["manage"], account_id="a1")
    store.add_token(hash_token("lim"), "partner", ["manage"], account_id="a1", products=[7])
    store.upsert_device(device_id="OPENMV_N6:bb", product_id=7, board="OPENMV_N6",
                        account_id="a2")
    store.upsert_device(device_id="OPENMV_N6:cc", product_id=9, board="OPENMV_N6",
                        account_id="a1")
    assert c.delete("/api/v1/admin/devices/OPENMV_N6:bb",
                    headers={"Authorization": "Bearer acct"}).status_code == 404
    assert c.delete("/api/v1/admin/devices/OPENMV_N6:cc",
                    headers={"Authorization": "Bearer lim"}).status_code == 404


def test_the_migration_does_not_orphan_a_running_servers_accounts(tmp_path):
    """The upgrade path, which is the part of this change that could break production.

    An account made before ownership existed still knows who made it -- `account.create`
    recorded the calling credential as its actor -- and an `accounts` token that could
    manage every account a moment ago keeps that authority. Only NEW tokens get the
    narrower scope."""
    from openmv_ota.server import metastore as M

    db = str(tmp_path / "before.db")
    full = M._MIGRATIONS
    try:
        M._MIGRATIONS = full[:23]                       # the schema before v24
        old = M.SqliteMetadataStore(db)
        assert old.migrate() == 23
        # the pre-v24 INSERT, written out: add_account now names columns that the old
        # schema does not have
        old.execute("INSERT INTO accounts (account_id, name, created_at) VALUES (?,?,?)",
                    ("acct_live", "Acme", "2026-01-01T00:00:00+00:00"))
        old.execute("INSERT INTO audit (seq, ts, actor, action, entity_type, entity_id, "
                    "data, prev_hash, entry_hash, account_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (1, "2026-01-01T00:00:00+00:00", "website", "account.create", "account",
                     "acct_live", "{}", "", "x", ""))
        old.add_token("h1", "website", ["publish", "manage", "observe", "accounts"])
        old.add_token("h2", "worker", ["manage", "observe"])
    finally:
        M._MIGRATIONS = full

    store = M.SqliteMetadataStore(db)
    store.migrate()
    assert store.get_account("acct_live")["created_by"] == "website"
    assert store.list_accounts(created_by="website")[0]["account_id"] == "acct_live"
    assert "accounts.all" in store.get_token("h1")["scopes"]     # authority preserved
    assert "accounts.all" not in store.get_token("h2")["scopes"]  # and not handed out


def test_a_camera_that_comes_back_enrols_as_a_new_device(tmp_path):
    """Forgetting is about the fleet, not about entitlement: the binding goes, so the
    next check-in learns one afresh rather than inheriting the old account."""
    c, store = _app(tmp_path)
    store.add_token(hash_token("acct"), "ci", ["manage"], account_id="a1")
    store.upsert_device(device_id="OPENMV_N6:dd", product_id=7, board="OPENMV_N6",
                        account_id="a1")
    store.bind_device_account("OPENMV_N6:dd", "a1", source="admin")
    c.delete("/api/v1/admin/devices/OPENMV_N6:dd", headers={"Authorization": "Bearer acct"})

    store.bind_device_account("OPENMV_N6:dd", "a2", source="learned")
    assert store.device_account("OPENMV_N6:dd")["account_id"] == "a2"
    assert store.device_account("OPENMV_N6:dd")["source"] == "learned"

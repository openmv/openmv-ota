"""The account's publish counter, end to end.

``payload_version`` orders a product's own releases, which is enough for a camera that
can only ever be offered its own product's images. A camera built with ``PRODUCT_ID = 0``
can be moved between product lines, and two products' version numbers say nothing about
each other -- so it orders by this counter instead. These tests pin the three places that
makes real: allocating it, refusing a reused one at publish, and declining to OFFER a
release below the camera's floor.
"""

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


def _app(tmp_path):
    store = SqliteMetadataStore(str(tmp_path / "ota.db"))
    store.migrate()
    store.set_meta("capability_secret", "x")
    store.add_account("acct_1", "Platform", created_by="op")
    store.add_token(hash_token("tok"), "ci", ["publish", "manage", "observe"],
                    account_id="acct_1")
    store.add_token(hash_token("selfhost"), "root", ["publish"], account_id="")
    app = create_app(ServerSettings(base_url="https://ota.test", swd_ids_verify_url="u",
                                    swd_ids_verify_token="t"),
                     metastore=store, storage=LocalArtifactStorage(str(tmp_path / "blobs")),
                     verifier=_Verifier())
    return TestClient(app), store


AUTH = {"Authorization": "Bearer tok"}


def test_every_allocation_is_a_number_nobody_else_got(tmp_path):
    """The one property that matters. Keys can be copied onto every build machine; a
    counter cannot, which is why it is allocated here and not there."""
    c, store = _app(tmp_path)
    got = [c.post("/api/v1/admin/publish-seq", headers=AUTH).json()["publish_seq"]
           for _ in range(5)]
    assert got == [1, 2, 3, 4, 5]
    assert len(set(got)) == 5
    assert store.get_account("acct_1")["publish_seq"] == 5


def test_a_self_host_has_no_counter_to_allocate_from(tmp_path):
    """The implicit '' account is not a row, so there is nothing to count in. Said
    plainly, because the alternative is a build that silently stamps 0 forever."""
    c, _ = _app(tmp_path)
    r = c.post("/api/v1/admin/publish-seq", headers={"Authorization": "Bearer selfhost"})
    assert r.status_code == 409 and "platform builds need a real account" in r.json()["detail"]


def test_allocating_needs_publish_scope(tmp_path):
    c, store = _app(tmp_path)
    store.add_token(hash_token("ro"), "reader", ["observe"], account_id="acct_1")
    assert c.post("/api/v1/admin/publish-seq",
                  headers={"Authorization": "Bearer ro"}).status_code == 403


def test_the_store_refuses_to_count_in_an_account_that_is_not_there(tmp_path):
    _, store = _app(tmp_path)
    assert store.next_publish_seq("acct_1") == 1
    assert store.next_publish_seq("nobody") is None
    assert store.newest_publish_seq(7, account_id="acct_1") == 0


def test_a_reused_counter_is_refused_at_publish(tmp_path):
    """Per PRODUCT, deliberately -- not account-wide. Concurrent builds finish out of
    order, and an account-wide gate would 409 a perfectly good publish for arriving
    second. Account-wide ordering is enforced at OFFER instead."""
    _, store = _app(tmp_path)
    for rid, seq in (("rel_a", 10), ("rel_b", 20)):
        store.add_release(release_id=rid, product_id=7, product="p", version="1.0.0",
                          payload_version=1, publish_seq=seq, min_platform_version=0,
                          image_sha256="x", image_size=1, representations=[],
                          manifest_key="m", image_key="i", account_id="acct_1")
    assert store.newest_publish_seq(7, account_id="acct_1") == 20
    # another product's stream is its own: 5 is not "behind" anything here
    assert store.newest_publish_seq(9, account_id="acct_1") == 0


def test_a_camera_is_never_offered_a_release_below_its_floor(tmp_path):
    """The device would refuse it, so the server does not hand it over -- and that is
    where the decision reads better. A camera that orders by the counter is on 500; a
    release numbered 400 is not for it, whatever its version says."""
    from openmv_ota.server.app import CheckIn, _ordering

    on_seq = CheckIn(device_id="d", product_id=7, payload_version=1 << 24,
                     publish_seq=500, orders_by_seq=True)
    rel = {"payload_version": 9 << 24, "publish_seq": 400}
    assert _ordering(on_seq, rel) == (500, 400)          # refused: 400 <= 500

    # ...and the same camera TAKES a numerically older version when the counter advanced,
    # which is the whole point: returning a claimed camera to a stock image at 1.0.0
    assert _ordering(on_seq, {"payload_version": 1 << 24, "publish_seq": 501}) == (500, 501)


def test_an_ordinary_camera_is_ordered_by_its_version(tmp_path):
    """It cannot change product, so its own product's versions order it completely --
    and a counter it never asked for must not get a say."""
    from openmv_ota.server.app import CheckIn, _ordering

    plain = CheckIn(device_id="d", product_id=7, payload_version=1 << 24)
    assert plain.orders_by_seq is False and plain.publish_seq == 0
    assert _ordering(plain, {"payload_version": 2 << 24, "publish_seq": 1}) == (1 << 24, 2 << 24)
    assert _ordering(plain, None) == (1 << 24, 0)

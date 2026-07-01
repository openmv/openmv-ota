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
    def __init__(self, registered=True, owner_ref="o1"):
        self._reg = Registration(registered, "N6", owner_ref)
        self.calls = 0

    def verify(self, board, device_id):
        self.calls += 1
        return self._reg


def _app(tmp_path, *, registered=True, base_url="https://ota.test", rate=0):
    store = SqliteMetadataStore(str(tmp_path / "ota.db"))
    store.migrate()
    store.set_meta("cohort_salt", SECRET)
    storage = LocalArtifactStorage(str(tmp_path / "blobs"))
    settings = ServerSettings(base_url=base_url, checkin_rate_per_min=rate,
                              swd_ids_verify_url="u", swd_ids_verify_token="t")
    verifier = _Verifier(registered)
    app = create_app(settings, storage=storage, metastore=store, verifier=verifier)
    return app, store, storage, verifier


def _seed(store, *, pv=0x02000000, percent=100, storage=None, manifest=b"MANI", image=b"IMG"):
    store.add_release(release_id="rel1", board="OPENMV_N6", board_id=7, product="P", version="2.0.0",
                      payload_version=pv, min_platform_version=0, image_sha256="ab" * 32,
                      image_size=len(image),
                      representations=[{"format": "full", "url": "OPENMV_N6-ota.img.gz",
                                        "size": len(image)}],
                      manifest_key="manifest/rel1", image_key="image/rel1")
    store.add_rollout(rollout_id="ro1", release_id="rel1", board="OPENMV_N6", cohort="__default__",
                      percent=percent)
    if storage is not None:
        storage.put("manifest/rel1", manifest, "application/octet-stream")
        storage.put("image/rel1", image, "application/gzip")


def _checkin(dev="dev1", board="OPENMV_N6", pv=0x01000000, **kw):
    return {"device_id": dev, "board": board, "payload_version": pv, **kw}


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


def test_not_in_staged_percent(tmp_path):
    app, store, storage, v = _app(tmp_path)
    _seed(store, pv=0x02000000, percent=0)
    assert TestClient(app).post("/api/v1/check", json=_checkin(pv=1)).json()["update"] is False


def test_anti_rollback_not_offered(tmp_path):
    app, store, storage, v = _app(tmp_path)
    _seed(store, pv=0x02000000, percent=100)
    assert TestClient(app).post("/api/v1/check", json=_checkin(pv=0x02000000)).json()["update"] is False


def test_rollout_pointing_at_missing_release(tmp_path):
    app, store, storage, v = _app(tmp_path)
    store.add_rollout(rollout_id="ro1", release_id="ghost", board="OPENMV_N6",
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

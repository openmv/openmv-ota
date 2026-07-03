"""Release publish: manifest-derived metadata, artifact consistency, anti-rollback."""

from __future__ import annotations

import gzip
import hashlib

from fastapi.testclient import TestClient

from openmv_ota.ota import delta as delta_codec
from openmv_ota.ota.algorithms import ES256
from openmv_ota.ota.manifest import DELTA_FORMAT, Manifest, pack_manifest
from openmv_ota.server.app import create_app
from openmv_ota.server.auth import hash_token
from openmv_ota.server.metastore import SqliteMetadataStore
from openmv_ota.server.settings import ServerSettings
from openmv_ota.server.storage import LocalArtifactStorage
from openmv_ota.server.verify import Registration

BID = 7
AUTH = {"Authorization": "Bearer tok"}


class _Verifier:
    def verify(self, board, device_id):
        return Registration(True)


def _app(tmp_path, scopes=("publish", "observe"), account=""):
    store = SqliteMetadataStore(str(tmp_path / "ota.db"))
    store.migrate()
    store.set_meta("cohort_salt", "x")
    store.add_token(hash_token("tok"), "ci", list(scopes), account_id=account)
    storage = LocalArtifactStorage(str(tmp_path / "blobs"))
    app = create_app(ServerSettings(base_url="https://ota.test", swd_ids_verify_url="u",
                                    swd_ids_verify_token="t"),
                     metastore=store, storage=storage, verifier=_Verifier())
    return app, store, storage


def _gz(b):
    return gzip.compress(b, mtime=0)          # mtime=0 -> deterministic (no header timestamp)


def _body(image, *, pv=0x02000000, size=None, with_delta=False):
    reps = [{"format": "full", "url": "x-ota.img.gz", "size": len(_gz(image))}]
    if with_delta:
        reps.append({"format": DELTA_FORMAT, "url": "x-ota.delta.gz", "size": 1,
                     "base_payload_version": 0x01000000})
    return {"schema": 1, "product_id": BID, "product": "P", "version": "2.0.0", "payload_version": pv,
            "min_platform_version": 0, "size": size if size is not None else len(image),
            "sha256": hashlib.sha256(image).hexdigest(), "representations": reps}


def _manifest(body):
    return pack_manifest(Manifest(body=body, key_id=0x0100, sig_alg=ES256, signature=b"\x00" * 64))


def _files(manifest, image_gz, delta_gz=None):
    files = {"manifest": ("manifest.bin", manifest, "application/octet-stream"),
             "image": ("img.gz", image_gz, "application/gzip")}
    if delta_gz is not None:
        files["delta"] = ("delta.gz", delta_gz, "application/gzip")
    return files


def _post(app, manifest, image_gz, delta_gz=None, query=""):
    return TestClient(app).post("/api/v1/admin/releases" + query, headers=AUTH,
                                files=_files(manifest, image_gz, delta_gz))


# --- happy paths ----------------------------------------------------------------------------

def test_publish_full_release(tmp_path):
    app, store, storage = _app(tmp_path)
    img = b"\xA5" * 64
    r = _post(app, _manifest(_body(img)), _gz(img))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["product_id"] == BID and body["payload_version"] == 0x02000000
    assert body["representations"] == ["full"]
    rel = store.get_release(body["release_id"])
    assert rel["image_sha256"] == hashlib.sha256(img).hexdigest()
    assert storage.get(rel["manifest_key"]) == _manifest(_body(img))
    assert storage.get(rel["image_key"]) == _gz(img)
    assert any(e["action"] == "release.publish" for e in store.read_audit())


def test_publish_with_delta(tmp_path):
    app, store, storage = _app(tmp_path)
    img = b"\xA5" * 128
    patch = delta_codec.make_delta(b"\x00" * 128, img)
    r = _post(app, _manifest(_body(img, with_delta=True)), _gz(img), _gz(patch))
    assert r.status_code == 200, r.text
    assert r.json()["representations"] == ["full", DELTA_FORMAT]
    rel = store.get_release(r.json()["release_id"])
    assert rel["delta_key"] and storage.get(rel["delta_key"]) == _gz(patch)


# --- auth + scopes --------------------------------------------------------------------------

def test_publish_no_token_401(tmp_path):
    app, store, storage = _app(tmp_path)
    img = b"\xA5" * 64
    assert TestClient(app).post("/api/v1/admin/releases",
                                files=_files(_manifest(_body(img)), _gz(img))).status_code == 401


def test_publish_wrong_scope_403(tmp_path):
    app, store, storage = _app(tmp_path, scopes=("observe",))
    img = b"\xA5" * 64
    assert _post(app, _manifest(_body(img)), _gz(img)).status_code == 403


def test_publish_account_must_match_token_403(tmp_path):
    # the token acts for acctA, but the signed manifest is stamped for a different ('') account
    app, store, storage = _app(tmp_path, account="acctA")
    img = b"\xA5" * 64
    r = _post(app, _manifest(_body(img)), _gz(img))            # manifest account_id defaults to ''
    assert r.status_code == 403 and "does not match" in r.json()["detail"]


def test_publish_account_match_ok(tmp_path):
    app, store, storage = _app(tmp_path, account="acctA")
    img = b"\xA5" * 64
    body = _body(img)
    body["account_id"] = "acctA"
    r = _post(app, _manifest(body), _gz(img))
    assert r.status_code == 200
    assert store.get_release(r.json()["release_id"])["account_id"] == "acctA"


# --- validation -----------------------------------------------------------------------------

def test_publish_bad_manifest_400(tmp_path):
    app, store, storage = _app(tmp_path)
    r = _post(app, b"garbage", _gz(b"\xA5" * 64))
    assert r.status_code == 400 and "bad manifest" in r.json()["detail"]


def test_publish_no_full_representation_400(tmp_path):
    app, store, storage = _app(tmp_path)
    img = b"\xA5" * 64
    body = _body(img)
    body["representations"] = [{"format": DELTA_FORMAT, "url": "d.gz", "size": 1,
                                "base_payload_version": 1}]
    r = _post(app, _manifest(body), _gz(img))
    assert r.status_code == 400 and "no 'full'" in r.json()["detail"]


def test_publish_image_not_gzip_400(tmp_path):
    app, store, storage = _app(tmp_path)
    img = b"\xA5" * 64
    r = _post(app, _manifest(_body(img)), b"not gzip")
    assert r.status_code == 400 and "not gzip" in r.json()["detail"]


def test_publish_sha_mismatch_400(tmp_path):
    app, store, storage = _app(tmp_path)
    img = b"\xA5" * 64
    r = _post(app, _manifest(_body(img)), _gz(b"\xFF" * 64))    # same size, wrong content
    assert r.status_code == 400 and "sha256" in r.json()["detail"]


def test_publish_size_mismatch_400(tmp_path):
    app, store, storage = _app(tmp_path)
    img = b"\xA5" * 64
    r = _post(app, _manifest(_body(img, size=999)), _gz(img))   # correct sha, wrong declared size
    assert r.status_code == 400 and "size does not match" in r.json()["detail"]


def test_publish_delta_declared_but_missing_400(tmp_path):
    app, store, storage = _app(tmp_path)
    img = b"\xA5" * 64
    r = _post(app, _manifest(_body(img, with_delta=True)), _gz(img))
    assert r.status_code == 400 and "declares a delta" in r.json()["detail"]


def test_publish_delta_uploaded_but_not_declared_400(tmp_path):
    app, store, storage = _app(tmp_path)
    img = b"\xA5" * 64
    patch = delta_codec.make_delta(b"\x00" * 64, img)
    r = _post(app, _manifest(_body(img)), _gz(img), _gz(patch))
    assert r.status_code == 400 and "declares none" in r.json()["detail"]


def test_publish_delta_not_gzip_400(tmp_path):
    app, store, storage = _app(tmp_path)
    img = b"\xA5" * 64
    r = _post(app, _manifest(_body(img, with_delta=True)), _gz(img), b"not gzip")
    assert r.status_code == 400 and "delta is not gzip" in r.json()["detail"]


def test_publish_delta_target_size_mismatch_400(tmp_path):
    app, store, storage = _app(tmp_path)
    img = b"\xA5" * 128
    wrong = delta_codec.make_delta(b"\x00" * 64, b"\xA5" * 64)   # target 64 != 128
    r = _post(app, _manifest(_body(img, with_delta=True)), _gz(img), _gz(wrong))
    assert r.status_code == 400 and "target size" in r.json()["detail"]


def test_publish_delta_malformed_400(tmp_path):
    app, store, storage = _app(tmp_path)
    img = b"\xA5" * 64
    r = _post(app, _manifest(_body(img, with_delta=True)), _gz(img), _gz(b"not-an-ocdl-patch"))
    assert r.status_code == 400 and "malformed" in r.json()["detail"]


def test_publish_anti_rollback_409_and_override(tmp_path):
    app, store, storage = _app(tmp_path)
    img = b"\xA5" * 64
    assert _post(app, _manifest(_body(img, pv=0x02000000)), _gz(img)).status_code == 200
    assert _post(app, _manifest(_body(img, pv=0x02000000)), _gz(img)).status_code == 409
    assert _post(app, _manifest(_body(img, pv=0x02000000)), _gz(img),
                 query="?allow_republish=true").status_code == 200
    assert _post(app, _manifest(_body(img, pv=0x02010000)), _gz(img)).status_code == 200

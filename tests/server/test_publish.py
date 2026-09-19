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
    store.set_meta("capability_secret", "x")
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


DELTA_NAME = "x-ota.delta.gz"          # must equal the rep url the manifest declares
ENC_DELTA_NAME = "x-ota.delta.gz.enc"  # ...and the encrypted release declares this one


def _files(manifest, image_gz, delta_gz=None, delta_name=DELTA_NAME):
    files = {"manifest": ("manifest.bin", manifest, "application/octet-stream"),
             "image": ("img.gz", image_gz, "application/gzip")}
    if delta_gz is not None:
        files["delta"] = (delta_name, delta_gz, "application/gzip")
    return files


def _post(app, manifest, image_gz, delta_gz=None, query="", delta_name=DELTA_NAME):
    return TestClient(app).post("/api/v1/admin/releases" + query, headers=AUTH,
                                files=_files(manifest, image_gz, delta_gz, delta_name))


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


def test_a_product_limited_token_publishes_only_into_its_own_products(tmp_path):
    """The manifest names the product, so publish is a write into one. A credential
    limited to other products is answered 404 -- the same as a product that does not
    exist, so the error cannot be used to map the account's product ids."""
    app, store, _ = _app(tmp_path, account="acct")
    store.add_token(hash_token("limited"), "partner", ["publish", "observe"],
                    account_id="acct", products=[BID + 1])     # NOT the BID we publish
    img = b"\xA5" * 64
    body = _body(img)
    body["account_id"] = "acct"
    files = _files(_manifest(body), _gz(img))
    resp = TestClient(app).post("/api/v1/admin/releases", files=files,
                                headers={"Authorization": "Bearer limited"})
    assert resp.status_code == 404
    assert store.list_releases() == []                          # nothing stored

    # the same token publishing into a product it DOES hold goes through
    store.add_token(hash_token("mine"), "partner2", ["publish", "observe"],
                    account_id="acct", products=[BID])
    ok = TestClient(app).post("/api/v1/admin/releases", files=_files(_manifest(body), _gz(img)),
                              headers={"Authorization": "Bearer mine"})
    assert ok.status_code == 200


def test_a_colliding_product_id_is_refused_not_merged(tmp_path):
    """A product id is crc32("<product>:<board>"), so two product names CAN hash to the
    same id -- a real prospect for a platform minting thousands of them. Merging them
    would offer one product line's firmware to the other's devices, silently. The second
    name is refused with the id and both names in the message, and nothing is stored."""
    app, store, _ = _app(tmp_path)
    img = b"\xA5" * 64
    assert _post(app, _manifest(_body(img)), _gz(img)).status_code == 200      # product "P"

    other = _body(img, pv=0x02010000)
    other["product"] = "Q"                       # same BID, different product name
    resp = _post(app, _manifest(other), _gz(other and img))
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "'P'" in detail and "'Q'" in detail and str(BID) in detail
    assert "product_id" in detail                # tells them how to fix it
    assert store.list_releases(product_id=BID) and len(store.list_releases()) == 1

    # The same name keeps publishing: this guards collisions, not new versions.
    assert _post(app, _manifest(_body(img, pv=0x02020000)), _gz(img)).status_code == 200


def test_publish_stores_dev_flag(tmp_path):
    app, store, storage = _app(tmp_path)
    img = b"\xA5" * 64
    body = _body(img)
    body["dev"] = True                                     # a dev-signed manifest
    r = _post(app, _manifest(body), _gz(img))
    assert r.status_code == 200 and store.get_release(r.json()["release_id"])["dev"] == 1
    # a normal release defaults to dev=0
    assert store.get_release(_post(app, _manifest(_body(img, pv=0x02010000)),
                                   _gz(img)).json()["release_id"])["dev"] == 0


def test_publish_with_delta(tmp_path):
    app, store, storage = _app(tmp_path)
    img = b"\xA5" * 128
    patch = delta_codec.make_delta(b"\x00" * 128, img)
    r = _post(app, _manifest(_body(img, with_delta=True)), _gz(img), _gz(patch))
    assert r.status_code == 200, r.text
    assert r.json()["representations"] == ["full", DELTA_FORMAT]
    rel = store.get_release(r.json()["release_id"])
    # stored under the name the manifest declares -- that is how the gateway finds it, and
    # how a release can carry several deltas without them overwriting each other
    assert storage.get("artifacts/%s/%s" % (rel["release_id"], DELTA_NAME)) == _gz(patch)


# --- auth + scopes --------------------------------------------------------------------------

def test_an_upload_larger_than_the_limit_is_refused_not_read(tmp_path):
    """`await upload.read()` allocates whatever the caller sent, and a publish token is
    a tenant's credential on a server every other fleet shares -- so without a ceiling
    one token is an out-of-memory button for all of them. It is the rule the device code
    lives by, applied where the server was not applying it."""
    store = SqliteMetadataStore(str(tmp_path / "ota.db"))
    store.migrate()
    store.set_meta("capability_secret", "x")
    store.add_token(hash_token("tok"), "ci", ["publish", "observe"])
    storage = LocalArtifactStorage(str(tmp_path / "blobs"))
    app = create_app(ServerSettings(base_url="https://ota.test", swd_ids_verify_url="u",
                                    swd_ids_verify_token="t",
                                    max_image_bytes=2048, max_manifest_bytes=512),
                     metastore=store, storage=storage, verifier=_Verifier())
    img = b"\xA5" * 64
    big = b"\x00" * 4096
    r = TestClient(app).post("/api/v1/admin/releases", headers=AUTH,
                             files=_files(_manifest(_body(img)), big))
    assert r.status_code == 413 and "image is larger" in r.json()["detail"]
    r = TestClient(app).post("/api/v1/admin/releases", headers=AUTH,
                             files=_files(big, _gz(img)))
    assert r.status_code == 413 and "manifest is larger" in r.json()["detail"]
    assert store.list_releases() == []          # nothing was stored on the way to refusing
    # and the ordinary case still goes through
    assert _post(app, _manifest(_body(img)), _gz(img)).status_code == 200


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


def test_publish_several_deltas_are_stored_and_served_separately(tmp_path):
    """The bug this exists for: with one delta_key per release, every ocdl rep resolved to the
    SAME stored object, so a device asking for the 1.1.0 patch got the 1.0.0 patch, failed its
    sha256, and never installed. A release now carries one delta per base version."""
    from openmv_ota.server import capability

    app, store, storage = _app(tmp_path)
    img = b"IMAGE-BYTES" * 40
    p0 = delta_codec.make_delta(b"\x00" * 128, img)
    p1 = delta_codec.make_delta(b"\x11" * 128, img)
    body = _body(img)
    body["representations"] += [
        {"format": DELTA_FORMAT, "url": "n6-ota.delta-1.0.0.gz", "size": 1,
         "base_payload_version": 0x01000000},
        {"format": DELTA_FORMAT, "url": "n6-ota.delta-1.1.0.gz", "size": 1,
         "base_payload_version": 0x01010000},
    ]
    files = {"manifest": ("manifest.bin", _manifest(body), "application/octet-stream"),
             "image": ("image.gz", _gz(img), "application/gzip")}
    r = TestClient(app).post(
        "/api/v1/admin/releases", headers=AUTH,
        files=[("manifest", files["manifest"]), ("image", files["image"]),
               ("delta", ("n6-ota.delta-1.0.0.gz", _gz(p0), "application/gzip")),
               ("delta", ("n6-ota.delta-1.1.0.gz", _gz(p1), "application/gzip"))])
    assert r.status_code == 200, r.text
    rel_id = r.json()["release_id"]

    # each is stored under its own name...
    assert storage.get("artifacts/%s/n6-ota.delta-1.0.0.gz" % rel_id) == _gz(p0)
    assert storage.get("artifacts/%s/n6-ota.delta-1.1.0.gz" % rel_id) == _gz(p1)
    # ...and the capability gateway serves each the right bytes, which is the actual fix
    c = TestClient(app)
    tok = capability.mint(app.state.secret, rel_id)
    assert c.get("/d/%s/n6-ota.delta-1.0.0.gz" % tok).content == _gz(p0)
    assert c.get("/d/%s/n6-ota.delta-1.1.0.gz" % tok).content == _gz(p1)


def test_publish_delta_declared_but_missing_400(tmp_path):
    app, store, storage = _app(tmp_path)
    img = b"\xA5" * 64
    r = _post(app, _manifest(_body(img, with_delta=True)), _gz(img))
    assert r.status_code == 400 and "declares delta(s) not uploaded" in r.json()["detail"]


def test_publish_delta_uploaded_but_not_declared_400(tmp_path):
    app, store, storage = _app(tmp_path)
    img = b"\xA5" * 64
    patch = delta_codec.make_delta(b"\x00" * 64, img)
    r = _post(app, _manifest(_body(img)), _gz(img), _gz(patch))
    assert r.status_code == 400 and "does not declare" in r.json()["detail"]


def test_publish_delta_not_gzip_400(tmp_path):
    app, store, storage = _app(tmp_path)
    img = b"\xA5" * 64
    r = _post(app, _manifest(_body(img, with_delta=True)), _gz(img), b"not gzip")
    assert r.status_code == 400 and "is not gzip" in r.json()["detail"]


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


def test_publish_stores_and_records_the_sbom(tmp_path):
    """The SBOM rides beside the artifacts and its key lands on the release row -- the
    dependency evidence for exactly the bytes this release ships."""
    app, store, storage = _app(tmp_path)
    img = b"\xA5" * 64
    files = _files(_manifest(_body(img)), _gz(img))
    files["sbom"] = ("sbom.cdx.json", b'{"bomFormat": "CycloneDX"}', "application/json")
    r = TestClient(app).post("/api/v1/admin/releases", headers=AUTH, files=files)
    assert r.status_code == 200, r.text
    rel = store.get_release(r.json()["release_id"])
    assert rel["sbom_key"] == "sbom/%s/sbom.cdx.json" % r.json()["release_id"]
    assert storage.get(rel["sbom_key"]) == b'{"bomFormat": "CycloneDX"}'


def test_publish_rejects_a_non_json_sbom(tmp_path):
    """Validated as JSON only -- a schema gate would reject evidence over formatting, but
    bytes that are not even JSON are a wrong file, not evidence."""
    app, store, storage = _app(tmp_path)
    img = b"\xA5" * 64
    files = _files(_manifest(_body(img)), _gz(img))
    files["sbom"] = ("sbom.cdx.json", b"\x00not json", "application/json")
    r = TestClient(app).post("/api/v1/admin/releases", headers=AUTH, files=files)
    assert r.status_code == 400 and "sbom" in r.json()["detail"]


def test_publish_without_sbom_records_none(tmp_path):
    app, store, storage = _app(tmp_path)
    img = b"\xA5" * 64
    r = _post(app, _manifest(_body(img)), _gz(img))
    assert store.get_release(r.json()["release_id"])["sbom_key"] is None


# --- encrypted artifacts ----------------------------------------------------
#
# The server stores what a camera downloads, and that is ciphertext. It cannot check
# the image digest any more -- it cannot read the image -- so what it checks is the
# ciphertext digest out of the same signed manifest. The plaintext check moves to the
# device, which is the only party holding the key.

def _encrypted(image, *, pv=0x02000000, with_delta=False):
    """``(body, image_ciphertext, delta_ciphertext, keys)`` for an encrypted release."""
    from openmv_ota.ota import payload

    keys = {1: payload.new_key()}
    image_ct, image_enc = payload.encrypt_artifact(_gz(image), keys)
    reps = [{"format": "full", "url": "x-ota.img.gz.enc", "size": len(image_ct),
             "enc": image_enc}]
    delta_ct = None
    if with_delta:
        patch = _gz(delta_codec.make_delta(b"\x00" * len(image), image))
        delta_ct, delta_enc = payload.encrypt_artifact(patch, keys)
        reps.append({"format": DELTA_FORMAT, "url": "x-ota.delta.gz.enc",
                     "size": len(delta_ct), "base_payload_version": 0x01000000,
                     "base_body_sha256": "ab" * 32, "enc": delta_enc})
    body = {"schema": 1, "product_id": BID, "product": "P", "version": "2.0.0",
            "payload_version": pv, "min_platform_version": 0, "size": len(image),
            "sha256": hashlib.sha256(image).hexdigest(), "representations": reps}
    return body, image_ct, delta_ct, keys


def test_an_encrypted_release_publishes_and_the_store_never_sees_the_image(tmp_path):
    image = b"\xA5" * 512
    body, image_ct, _d, _k = _encrypted(image)
    app, store, storage = _app(tmp_path)
    resp = _post(app, _manifest(body), image_ct)
    assert resp.status_code == 200, resp.text

    rel = store.get_release(resp.json()["release_id"])
    stored = storage.get(rel["image_key"])
    assert stored == image_ct
    assert image not in stored                       # the bytes at rest are not the image
    assert _gz(image) not in stored


def test_an_encrypted_artifact_that_does_not_match_its_manifest_is_refused(tmp_path):
    """The check the server can still make: these bytes belong to this manifest."""
    image = b"\xA5" * 512
    body, image_ct, _d, _k = _encrypted(image)
    app, _store, _storage = _app(tmp_path)
    resp = _post(app, _manifest(body), image_ct[:-16] + b"\x00" * 16)
    assert resp.status_code == 400
    assert "sha256 does not match" in resp.json()["detail"]


def test_an_encrypted_artifact_of_the_wrong_length_is_refused(tmp_path):
    image = b"\xA5" * 512
    body, image_ct, _d, _k = _encrypted(image)
    body["representations"][0]["size"] = len(image_ct) + 16
    app, _store, _storage = _app(tmp_path)
    resp = _post(app, _manifest(body), image_ct)
    assert resp.status_code == 400
    assert "size does not match" in resp.json()["detail"]


def test_an_artifact_that_is_not_whole_blocks_is_refused(tmp_path):
    """A release nobody could decrypt is refused at publish, not discovered by a fleet."""
    image = b"\xA5" * 512
    body, image_ct, _d, _k = _encrypted(image)
    short = image_ct[:-1]
    body["representations"][0]["size"] = len(short)
    body["representations"][0]["enc"]["sha256"] = hashlib.sha256(short).hexdigest()
    app, _store, _storage = _app(tmp_path)
    resp = _post(app, _manifest(body), short)
    assert resp.status_code == 400
    assert "well-formed encrypted artifact" in resp.json()["detail"]


def test_a_declared_plaintext_length_past_the_ciphertext_is_refused(tmp_path):
    image = b"\xA5" * 512
    body, image_ct, _d, _k = _encrypted(image)
    body["representations"][0]["enc"]["size"] = len(image_ct) + 1
    app, _store, _storage = _app(tmp_path)
    resp = _post(app, _manifest(body), image_ct)
    assert resp.status_code == 400
    assert "well-formed encrypted artifact" in resp.json()["detail"]


def test_an_encrypted_delta_is_checked_the_same_way(tmp_path):
    image = b"\xA5" * 512
    body, image_ct, delta_ct, _k = _encrypted(image, with_delta=True)
    app, _store, _storage = _app(tmp_path)
    ok = _post(app, _manifest(body), image_ct, delta_ct, delta_name=ENC_DELTA_NAME)
    assert ok.status_code == 200, ok.text

    bad = _post(app, _manifest(body), image_ct, delta_ct[:-16] + b"\x00" * 16,
                query="?allow_republish=1", delta_name=ENC_DELTA_NAME)
    assert bad.status_code == 400
    assert "x-ota.delta.gz.enc sha256 does not match" in bad.json()["detail"]


def test_a_reused_publish_counter_is_refused(tmp_path):
    """Per PRODUCT and not account-wide, on purpose: concurrent builds finish out of
    order, and an account-wide gate would 409 a publish for the crime of arriving second.
    What must never get through is two artifacts of one product sharing a number -- a
    camera cannot order those, and the ordering is the whole anti-rollback story for a
    fleet whose cameras can change product."""
    app, store, _storage = _app(tmp_path)
    img = b"\xA5" * 64

    body = _body(img, pv=0x02000000)
    body["publish_seq"] = 500
    assert _post(app, _manifest(body), _gz(img)).status_code == 200
    assert store.newest_publish_seq(BID) == 500

    # same number again -> refused, and the message says what to do about it
    again = _body(img, pv=0x03000000)
    again["publish_seq"] = 500
    r = _post(app, _manifest(again), _gz(img))
    assert r.status_code == 409 and "take a fresh one" in r.json()["detail"]

    # ...and a higher one goes through
    ahead = _body(img, pv=0x03000000)
    ahead["publish_seq"] = 501
    assert _post(app, _manifest(ahead), _gz(img)).status_code == 200

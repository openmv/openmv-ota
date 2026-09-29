"""Fuzz the release publish endpoint (``POST /api/v1/admin/releases``) with hostile manifest
bodies and artifacts.

The server never verifies the manifest's signature -- the device does -- so to the server the
body is whatever the uploader wrote. Every answer must be a deliberate status (200, or a
4xx that stored nothing); a 500 means a handler tripped over a shape it never checked."""

from __future__ import annotations

import gzip
import hashlib
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st

from openmv_ota.ota import delta as D
from openmv_ota.ota.algorithms import ES256
from openmv_ota.ota.manifest import Manifest, pack_manifest
from openmv_ota.server.app import create_app
from openmv_ota.server.auth import hash_token
from openmv_ota.server.metastore import SqliteMetadataStore
from openmv_ota.server.settings import ServerSettings
from openmv_ota.server.storage import LocalArtifactStorage
from openmv_ota.server.verify import Registration
from tests.fuzz import fuzzlib as F

_IMG = b"\xa5" * 256
_APP = {}


class _Verifier:
    def verify(self, board, device_id):
        return Registration(True)


def _client():
    """One app for the whole property (a fresh one per example would dominate the run); a
    release that lands just raises the anti-rollback floor, which later examples meet as 409."""
    if "c" not in _APP:
        root = Path(tempfile.mkdtemp(prefix="fuzz-publish-"))
        store = SqliteMetadataStore(str(root / "ota.db"))
        store.migrate()
        store.set_meta("capability_secret", "x")
        store.add_token(hash_token("tok"), "ci", ["publish", "observe"], account_id="")
        app = create_app(ServerSettings(base_url="https://ota.test", swd_ids_verify_url="u",
                                        swd_ids_verify_token="t"),
                         metastore=store, storage=LocalArtifactStorage(str(root / "blobs")),
                         verifier=_Verifier())
        _APP["c"] = TestClient(app)
        _APP["root"] = root
    return _APP["c"]


def _gz(b):
    return gzip.compress(b, mtime=0)


_FIELDS = ("schema", "product_id", "product", "version", "payload_version", "publish_seq",
           "min_platform_version", "size", "sha256", "representations", "account_id")


@st.composite
def bodies(draw):
    body = F.manifest_body(_IMG, payload_version=draw(st.integers(1, 1 << 40)))
    body["representations"].append({"format": "ocdl", "url": "x.delta.gz", "size": 5,
                                    "base_payload_version": 1})
    for _ in range(draw(st.integers(0, 4))):
        field = draw(st.sampled_from(_FIELDS))
        action = draw(st.sampled_from(("replace", "delete", "rep")))
        if action == "delete":
            body.pop(field, None)
        elif action == "replace":
            body[field] = draw(F.json_values)
        else:
            reps = body.get("representations")
            if isinstance(reps, list) and reps and isinstance(reps[0], dict):
                key = draw(st.sampled_from(("format", "url", "size", "enc")))
                reps[0][key] = draw(F.json_values | st.sampled_from(
                    ["..", "../x", "a/../b", "", "full", "ocdl"]))
    return body


_client()                                           # start-up outside any timed example

_image = st.sampled_from([_gz(_IMG), _gz(_IMG)[:-4], b"not gzip", _gz(_IMG) + b"x",
                          _gz(bytes(1 << 20))]) | st.binary(max_size=64)
_delta = st.sampled_from([_gz(D.make_delta(b"\x00" * 256, _IMG)), _gz(D.MAGIC + b"\xff" * 64),
                          _gz(b"OCDL"), b"junk"]) | st.binary(max_size=64)


@given(bodies(), _image, st.none() | _delta)
def test_publish_answers_deliberately_to_any_manifest(body, image, delta):
    m = pack_manifest(Manifest(body=body, key_id=0x0100, sig_alg=ES256,
                               signature=bytes(64)))
    files = [("manifest", ("manifest.bin", m, "application/octet-stream")),
             ("image", ("img.gz", image, "application/gzip"))]
    if delta is not None:
        files.append(("delta", ("x.delta.gz", delta, "application/gzip")))
    r = _client().post("/api/v1/admin/releases", headers={"Authorization": "Bearer tok"},
                       files=files)
    assert r.status_code in (200, 400, 403, 404, 409), (r.status_code, r.text)
    if r.status_code == 200:
        # accepted only if it is exactly the artifact the manifest describes
        assert image == _gz(_IMG) and body["sha256"] == hashlib.sha256(_IMG).hexdigest()
    # nothing ever lands outside a release's own directory
    root = _APP["root"] / "blobs"
    for p in root.rglob("*"):
        rel = p.relative_to(root).parts
        assert rel[0] in ("manifests", "artifacts") and (len(rel) < 2 or rel[1].startswith("rel_"))

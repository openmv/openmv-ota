"""ArtifactStorage: local (disk) + s3 (fake client), and the backend factory."""

from __future__ import annotations

import io
import sys

import pytest

from openmv_ota.server.errors import ServerError
from openmv_ota.server.settings import ServerSettings
from openmv_ota.server.storage import (
    LocalArtifactStorage,
    S3ArtifactStorage,
    build_storage,
)


def _settings(**kw):
    kw.setdefault("swd_ids_verify_url", "u")
    kw.setdefault("swd_ids_verify_token", "t")
    return ServerSettings(**kw)


# --- LocalArtifactStorage -------------------------------------------------------------------

def test_local_roundtrip(tmp_path):
    s = LocalArtifactStorage(tmp_path)
    assert s.exists("a/b.bin") is False
    s.put("a/b.bin", b"hello", "application/octet-stream")
    assert s.exists("a/b.bin") is True
    assert s.get("a/b.bin") == b"hello"
    with s.open("a/b.bin") as fh:
        assert fh.read() == b"hello"
    assert s.url_for("a/b.bin") is None            # no offload; app streams it
    s.delete("a/b.bin")
    assert s.exists("a/b.bin") is False
    s.delete("a/b.bin")                            # idempotent


def test_local_missing_raises(tmp_path):
    s = LocalArtifactStorage(tmp_path)
    with pytest.raises(ServerError, match="no such artifact"):
        s.get("nope")
    with pytest.raises(ServerError, match="no such artifact"):
        s.open("nope")


def test_local_rejects_path_traversal(tmp_path):
    s = LocalArtifactStorage(tmp_path)
    with pytest.raises(ServerError, match="bad artifact key"):
        s.put("../escape", b"x", "application/octet-stream")


# --- S3ArtifactStorage (fake boto3 client) --------------------------------------------------

class _FakeS3:
    def __init__(self):
        self.objs: dict = {}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objs[(Bucket, Key)] = (Body, ContentType)

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objs[(Bucket, Key)][0])}

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objs:
            raise KeyError("missing")
        return {}

    def delete_object(self, Bucket, Key):
        self.objs.pop((Bucket, Key), None)

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return "https://s3.example/%s?exp=%d" % (Params["Key"], ExpiresIn)


def test_s3_roundtrip_and_presign():
    s = S3ArtifactStorage("bkt", client=_FakeS3())
    assert s.exists("k") is False
    s.put("k", b"data", "application/gzip")
    assert s.exists("k") is True and s.get("k") == b"data"
    assert s.open("k").read() == b"data"
    url = s.url_for("k", expires=120)
    assert url.startswith("https://s3.example/k") and "exp=120" in url
    s.delete("k")
    assert s.exists("k") is False


def test_s3_missing_boto3_gives_install_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "boto3", None)   # force `import boto3` to fail
    with pytest.raises(ServerError, match="server-s3"):
        S3ArtifactStorage("bkt")


# --- build_storage --------------------------------------------------------------------------

def test_build_storage_local(tmp_path):
    assert isinstance(build_storage(_settings(storage_backend="local",
                                              storage_location=str(tmp_path))), LocalArtifactStorage)


def test_build_storage_s3_dispatches(monkeypatch):
    monkeypatch.setitem(sys.modules, "boto3", None)
    with pytest.raises(ServerError, match="server-s3"):     # proves it built an S3 backend
        build_storage(_settings(storage_backend="s3", s3_bucket="bkt"))


def test_build_storage_unknown_backend():
    with pytest.raises(ServerError, match="unknown storage backend"):
        build_storage(_settings(storage_backend="weird"))

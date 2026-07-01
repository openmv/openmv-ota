"""Artifact storage -- the already-signed release blobs (manifest, image, delta).

Two interchangeable backends behind one ABC, selected by settings so prod vs dev is a config flip:
``S3ArtifactStorage`` (R2/S3, prod -- ``url_for`` returns a short-lived presigned URL the app
302-redirects to) and ``LocalArtifactStorage`` (disk, dev/test -- ``url_for`` returns ``None`` so
the app streams the bytes). Keys are opaque paths the caller assigns; blobs are immutable.
"""

from __future__ import annotations

import abc
import io
from pathlib import Path
from typing import BinaryIO

from .errors import ServerError


class ArtifactStorage(abc.ABC):
    @abc.abstractmethod
    def put(self, key: str, data: bytes, content_type: str) -> None: ...

    @abc.abstractmethod
    def get(self, key: str) -> bytes: ...

    @abc.abstractmethod
    def open(self, key: str) -> BinaryIO: ...

    @abc.abstractmethod
    def exists(self, key: str) -> bool: ...

    @abc.abstractmethod
    def delete(self, key: str) -> None: ...

    @abc.abstractmethod
    def url_for(self, key: str, *, expires: int = 300) -> str | None:
        """A short-lived presigned URL to 302-redirect to, or ``None`` to stream via the app."""


class LocalArtifactStorage(ArtifactStorage):
    """Filesystem-backed (local dev / tests / single-box self-host)."""

    def __init__(self, root: str | Path):
        self._root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        p = (self._root / key).resolve()
        if p != self._root and not p.is_relative_to(self._root):
            raise ServerError("bad artifact key: %r" % key, exit_code=1)
        return p

    def put(self, key: str, data: bytes, content_type: str) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def get(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError:
            raise ServerError("no such artifact: %s" % key, exit_code=1) from None

    def open(self, key: str) -> BinaryIO:
        try:
            return self._path(key).open("rb")
        except FileNotFoundError:
            raise ServerError("no such artifact: %s" % key, exit_code=1) from None

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def url_for(self, key: str, *, expires: int = 300) -> str | None:
        return None                              # no offload; the app streams it


class S3ArtifactStorage(ArtifactStorage):
    """S3/R2-backed (prod). ``client`` is a boto3 S3 client; injectable for tests."""

    def __init__(self, bucket: str, *, client=None, endpoint_url: str = "", region: str = "",
                 access_key_id: str = "", secret_access_key: str = ""):
        if client is None:
            try:
                import boto3
            except ImportError:
                raise ServerError("the s3 storage backend needs boto3 -- "
                                  "pip install openmv-ota[server-s3]", exit_code=2) from None
            client = boto3.client(                                    # pragma: no cover
                "s3", endpoint_url=endpoint_url or None, region_name=region or None,
                aws_access_key_id=access_key_id or None,
                aws_secret_access_key=secret_access_key or None)
        self._bucket = bucket
        self._s3 = client

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self._s3.put_object(Bucket=self._bucket, Key=key, Body=data, ContentType=content_type)

    def get(self, key: str) -> bytes:
        return self._s3.get_object(Bucket=self._bucket, Key=key)["Body"].read()

    def open(self, key: str) -> BinaryIO:
        return io.BytesIO(self.get(key))

    def exists(self, key: str) -> bool:
        try:
            self._s3.head_object(Bucket=self._bucket, Key=key)
            return True
        except Exception:
            return False

    def delete(self, key: str) -> None:
        self._s3.delete_object(Bucket=self._bucket, Key=key)

    def url_for(self, key: str, *, expires: int = 300) -> str | None:
        return self._s3.generate_presigned_url(
            "get_object", Params={"Bucket": self._bucket, "Key": key}, ExpiresIn=expires)


def build_storage(settings) -> ArtifactStorage:
    """The ``ArtifactStorage`` for ``settings.storage_backend`` (``local`` | ``s3``)."""
    if settings.storage_backend == "local":
        return LocalArtifactStorage(settings.storage_location)
    if settings.storage_backend == "s3":
        return S3ArtifactStorage(
            settings.s3_bucket, endpoint_url=settings.s3_endpoint_url, region=settings.s3_region,
            access_key_id=settings.s3_access_key_id,
            secret_access_key=settings.s3_secret_access_key)
    raise ServerError("unknown storage backend: %r" % settings.storage_backend, exit_code=2)

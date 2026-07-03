"""Release publish -- ``POST /api/v1/admin/releases`` (multipart), scope ``release:write``.

The server derives **all** metadata from the *signed* manifest (never client-asserted JSON),
verifies the uploaded artifacts are consistent with it, applies publish-time anti-rollback, stores
the blobs immutably, and records the release. It never verifies the signature (the device does)
and never holds a key -- so it can refuse an inconsistent set, but it can't manufacture trust.
"""

from __future__ import annotations

import gzip
import hashlib

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile

from openmv_ota.ota import delta as delta_codec
from openmv_ota.ota.errors import OtaError
from openmv_ota.ota.manifest import DELTA_FORMAT, parse_manifest

from .admin import new_id
from .auth import Principal, require_scope

publish = APIRouter(prefix="/api/v1/admin")


def _gunzip(data: bytes) -> bytes | None:
    try:
        return gzip.decompress(data)
    except (OSError, EOFError):
        return None


def _rep(reps, fmt):
    for r in reps:
        if r["format"] == fmt:
            return r
    return None


def _verify_artifacts(body: dict, image_bytes: bytes, delta_bytes: bytes | None) -> None:
    """Refuse a set that doesn't match the signed manifest (raises HTTPException 400)."""
    reps = body["representations"]
    if _rep(reps, "full") is None:
        raise HTTPException(status_code=400, detail="manifest has no 'full' representation")
    raw = _gunzip(image_bytes)
    if raw is None:
        raise HTTPException(status_code=400, detail="image is not gzip")
    if hashlib.sha256(raw).hexdigest() != body["sha256"]:
        raise HTTPException(status_code=400, detail="image sha256 does not match the manifest")
    if len(raw) != body["size"]:
        raise HTTPException(status_code=400, detail="image size does not match the manifest")
    declares_delta = _rep(reps, DELTA_FORMAT) is not None
    if declares_delta and delta_bytes is None:
        raise HTTPException(status_code=400, detail="manifest declares a delta but none was uploaded")
    if delta_bytes is not None and not declares_delta:
        raise HTTPException(status_code=400, detail="a delta was uploaded but the manifest declares none")
    if declares_delta:
        patch = _gunzip(delta_bytes)
        if patch is None:
            raise HTTPException(status_code=400, detail="delta is not gzip")
        try:
            if delta_codec.target_size(patch) != body["size"]:
                raise HTTPException(status_code=400, detail="delta target size != manifest size")
        except OtaError:
            raise HTTPException(status_code=400, detail="delta is malformed") from None


@publish.post("/releases")
async def publish_release(request: Request, manifest: UploadFile = File(...),
                          image: UploadFile = File(...), delta: UploadFile | None = File(None),
                          allow_republish: bool = False,
                          principal: Principal = Depends(require_scope("release:write"))):
    ms = request.app.state.metastore
    storage = request.app.state.storage
    manifest_bytes = await manifest.read()
    try:
        body = parse_manifest(manifest_bytes).body
    except OtaError as e:
        raise HTTPException(status_code=400, detail="bad manifest: %s" % e) from None
    product_id, payload_version = body["product_id"], body["payload_version"]
    account_id = body.get("account_id", "")           # the maker's account (baked into the signed manifest)
    if account_id != principal.account_id:
        # you can only publish releases under your own account -- the signed manifest's account
        # must match the token's, so one tenant can't seed another's namespace.
        raise HTTPException(status_code=403, detail="manifest account_id does not match this token")

    latest = ms.latest_release_payload_version(product_id, account_id=account_id)
    if latest is not None and payload_version <= latest and not allow_republish:
        raise HTTPException(status_code=409, detail="payload_version %d <= latest %d "
                            "(pass allow_republish=true to override)" % (payload_version, latest))

    image_bytes = await image.read()
    delta_bytes = await delta.read() if delta is not None else None
    _verify_artifacts(body, image_bytes, delta_bytes)

    release_id = new_id("rel")
    reps = body["representations"]
    manifest_key = "manifests/%s/manifest.bin" % release_id
    image_key = "artifacts/%s/%s" % (release_id, _rep(reps, "full")["url"])
    storage.put(manifest_key, manifest_bytes, "application/octet-stream")
    storage.put(image_key, image_bytes, "application/gzip")
    delta_key = None
    if delta_bytes is not None:
        delta_key = "artifacts/%s/%s" % (release_id, _rep(reps, DELTA_FORMAT)["url"])
        storage.put(delta_key, delta_bytes, "application/gzip")

    ms.add_release(release_id=release_id, product_id=product_id, product=body.get("product"),
                   version=body.get("version"), payload_version=payload_version,
                   min_platform_version=body.get("min_platform_version", 0),
                   image_sha256=body["sha256"], image_size=body["size"], representations=reps,
                   manifest_key=manifest_key, image_key=image_key, delta_key=delta_key,
                   uploaded_by=principal.name, account_id=account_id)
    ms.append_audit(actor=principal.name, action="release.publish", entity_type="release",
                    entity_id=release_id, data={"product_id": product_id, "version": body.get("version"),
                                                "payload_version": payload_version},
                    account_id=account_id)
    return {"release_id": release_id, "product_id": product_id, "version": body.get("version"),
            "payload_version": payload_version, "representations": [r["format"] for r in reps]}

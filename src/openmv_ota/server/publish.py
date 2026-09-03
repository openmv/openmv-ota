"""Release publish -- ``POST /api/v1/admin/releases`` (multipart), scope ``publish``.

The server derives **all** metadata from the *signed* manifest (never client-asserted JSON),
verifies the uploaded artifacts are consistent with it, applies publish-time anti-rollback, stores
the blobs immutably, and records the release. It never verifies the signature (the device does)
and never holds a key -- so it can refuse an inconsistent set, but it can't manufacture trust.
"""

from __future__ import annotations

import gzip
import hashlib
import json

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile

from openmv_ota.ota import delta as delta_codec
from openmv_ota.ota.errors import OtaError
from openmv_ota.ota.manifest import DELTA_FORMAT, parse_manifest

from .admin import _label, new_id
from .auth import Principal, require_scope

from .schemas import Published

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


def _verify_artifacts(body: dict, image_bytes: bytes, deltas: dict) -> None:
    """Refuse a set that doesn't match the signed manifest (raises HTTPException 400).

    ``deltas`` is ``{filename: gzipped patch}``. The manifest is the contract: every ``ocdl``
    representation it declares must arrive, and nothing else may -- matched BY FILENAME, since
    a release now carries one delta per base version and a set matched only by count could
    store them under each other's names."""
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
    declared = {rep["url"].rsplit("/", 1)[-1] for rep in reps if rep["format"] == DELTA_FORMAT}
    missing = sorted(declared - set(deltas))
    if missing:
        raise HTTPException(status_code=400,
                            detail="manifest declares delta(s) not uploaded: %s"
                                   % ", ".join(missing))
    extra = sorted(set(deltas) - declared)
    if extra:
        raise HTTPException(status_code=400,
                            detail="delta(s) uploaded that the manifest does not declare: %s"
                                   % ", ".join(extra))
    for filename in sorted(declared):
        patch = _gunzip(deltas[filename])
        if patch is None:
            raise HTTPException(status_code=400, detail="%s is not gzip" % filename)
        try:
            if delta_codec.target_size(patch) != body["size"]:
                raise HTTPException(status_code=400,
                                    detail="%s target size != manifest size" % filename)
        except OtaError:
            raise HTTPException(status_code=400, detail="%s is malformed" % filename) from None


@publish.post("/releases", responses={200: {"model": Published}})
async def publish_release(request: Request, manifest: UploadFile = File(...),
                          image: UploadFile = File(...),
                          delta: list[UploadFile] | None = File(None),
                          sbom: UploadFile | None = File(None),
                          allow_republish: bool = False, display_name: str = "",
                          principal: Principal = Depends(require_scope("publish"))):
    ms = request.app.state.metastore
    storage = request.app.state.storage
    manifest_bytes = await manifest.read()
    try:
        body = parse_manifest(manifest_bytes).body
    except OtaError as e:
        raise HTTPException(status_code=400, detail="bad manifest: %s" % e) from None
    product_id, payload_version = body["product_id"], body["payload_version"]
    # A label only -- it lives beside, not in, the signed manifest, so it stays renamable.
    display_name = _label(display_name)
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
    # REPEATABLE. A release ships one delta per base version still in the field, because a
    # device patches against the release it is RUNNING -- one delta reaches only the devices
    # that never updated. Each is matched to its representation by filename.
    uploads = list(delta or [])
    deltas = {(u.filename or "").rsplit("/", 1)[-1]: await u.read() for u in uploads}
    _verify_artifacts(body, image_bytes, deltas)

    # The SBOM rides beside the artifacts when the client sends one: the dependency evidence
    # for the exact bytes this release ships, served per release instead of living only on the
    # build machine. Validated as JSON only -- the render is the build's job, and a schema gate
    # here would reject evidence over formatting.
    sbom_bytes = await sbom.read() if sbom is not None else None
    if sbom_bytes is not None:
        try:
            json.loads(sbom_bytes)
        except ValueError:
            raise HTTPException(status_code=400, detail="sbom is not JSON") from None

    release_id = new_id("rel")
    reps = body["representations"]
    manifest_key = "manifests/%s/manifest.bin" % release_id
    image_key = "artifacts/%s/%s" % (release_id, _rep(reps, "full")["url"])
    storage.put(manifest_key, manifest_bytes, "application/octet-stream")
    storage.put(image_key, image_bytes, "application/gzip")
    for filename, patch in deltas.items():
        storage.put("artifacts/%s/%s" % (release_id, filename), patch, "application/gzip")
    sbom_key = None
    if sbom_bytes is not None:
        sbom_key = "sbom/%s/sbom.cdx.json" % release_id
        storage.put(sbom_key, sbom_bytes, "application/json")

    ms.add_release(release_id=release_id, product_id=product_id, product=body.get("product"),
                   version=body.get("version"), payload_version=payload_version,
                   min_platform_version=body.get("min_platform_version", 0),
                   image_sha256=body["sha256"], image_size=body["size"], representations=reps,
                   manifest_key=manifest_key, image_key=image_key,
                   uploaded_by=principal.name, account_id=account_id,
                   dev=1 if body.get("dev") else 0,   # dev-signed provenance (visibility only)
                   sbom_key=sbom_key, display_name=display_name)
    ms.append_audit(actor=principal.name, action="release.publish", entity_type="release",
                    entity_id=release_id, data={"product_id": product_id, "version": body.get("version"),
                                                "payload_version": payload_version},
                    account_id=account_id)
    return {"release_id": release_id, "product_id": product_id, "version": body.get("version"),
            "payload_version": payload_version, "representations": [r["format"] for r in reps],
            "display_name": display_name}

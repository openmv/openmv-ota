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
import sys

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile

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
    full = _rep(reps, "full")
    if full is None:
        raise HTTPException(status_code=400, detail="manifest has no 'full' representation")
    if not _verify_encrypted(full, image_bytes, "image"):
        raw = _gunzip(image_bytes)
        if raw is None:
            raise HTTPException(status_code=400, detail="image is not gzip")
        if hashlib.sha256(raw).hexdigest() != body["sha256"]:
            raise HTTPException(status_code=400,
                                detail="image sha256 does not match the manifest")
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
    by_name = {rep["url"].rsplit("/", 1)[-1]: rep for rep in reps}
    for filename in sorted(declared):
        if _verify_encrypted(by_name[filename], deltas[filename], filename):
            continue
        patch = _gunzip(deltas[filename])
        if patch is None:
            raise HTTPException(status_code=400, detail="%s is not gzip" % filename)
        try:
            if delta_codec.target_size(patch) != body["size"]:
                raise HTTPException(status_code=400,
                                    detail="%s target size != manifest size" % filename)
        except OtaError:
            raise HTTPException(status_code=400, detail="%s is malformed" % filename) from None


def _verify_encrypted(rep: dict, data: bytes, what: str) -> bool:
    """Check an encrypted artifact against its signed manifest entry. ``False`` means
    this representation is not encrypted and the plaintext checks still apply.

    The server cannot read these bytes, and should not be able to -- that is the whole
    point -- so the digest it checks is the CIPHERTEXT's, out of the same signed
    manifest that carries the plaintext digest for the camera. That still catches
    exactly what this check was for: an upload that does not belong to the manifest it
    arrived with, or that was mangled on the way. What moves is WHERE the plaintext is
    verified -- on the device, which is the only party that can."""
    enc = rep.get("enc")
    if not isinstance(enc, dict):
        return False
    if hashlib.sha256(data).hexdigest() != enc.get("sha256"):
        raise HTTPException(status_code=400,
                            detail="%s sha256 does not match the manifest" % what)
    if len(data) != rep.get("size"):
        raise HTTPException(status_code=400,
                            detail="%s size does not match the manifest" % what)
    size = enc.get("size")
    if len(data) % 16 or not isinstance(size, int) or not 0 <= size <= len(data):
        raise HTTPException(status_code=400,
                            detail="%s is not a well-formed encrypted artifact" % what)
    return True


async def _read_capped(upload: UploadFile, limit: int, what: str) -> bytes:
    """Read an upload, refusing past ``limit``.

    `await upload.read()` allocates whatever the caller sent. On a server every tenant
    shares, that hands one publish token an out-of-memory button for everybody -- and it
    is the same rule the device code lives by, applied in the one place it was not.

    Content-Length is checked first because it is free, and then the read is capped
    anyway: the header is the uploader's claim about the uploader's own body."""
    read = 0
    chunks = []
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        read += len(chunk)
        if read > limit:
            raise HTTPException(status_code=413,
                                detail="%s is larger than the %d byte limit" % (what, limit))
        chunks.append(chunk)
    return b"".join(chunks)


@publish.post("/releases", responses={200: {"model": Published}})
async def publish_release(request: Request, background: BackgroundTasks,
                          manifest: UploadFile = File(...),
                          image: UploadFile = File(...),
                          delta: list[UploadFile] | None = File(None),
                          sbom: UploadFile | None = File(None),
                          allow_republish: bool = False, display_name: str = "",
                          principal: Principal = Depends(require_scope("publish"))):
    """Upload a signed release built by `openmv-ota build`, as `multipart/form-data`.

    The **manifest** is the contract and everything else is checked against it: the
    `image` must match its digest and size, each `delta` must name a base the manifest
    declares, and anything that fails is a 400 with nothing stored. The server never
    signs -- it distributes what your keys already signed, and it never holds them.

    The manifest carries the `product_id` and `payload_version`, so this call is also
    what brings a product into existence: publish a release for a product id and the
    product appears in `GET /api/v1/admin/products`, ready to be renamed and rolled
    out. `payload_version` must exceed the account's newest for that product, which is
    the anti-rollback floor; pass `allow_republish=true` to overwrite a version during
    development.

    Publishing does not stage anything: a release sits there until a rollout offers
    it. `openmv-ota client release publish --percent` looks like one step, but the CLI
    is making two calls -- this one, then `POST /api/v1/admin/rollouts` with the
    `release_id` it got back. An integration does the same two calls.

    The response carries `release_id`, `product_id`, `version` and the
    `representations` the release covers; keep the `release_id` if you intend to roll
    it out, pin it, or read its SBOM."""
    ms = request.app.state.metastore
    storage = request.app.state.storage
    settings = request.app.state.settings
    manifest_bytes = await _read_capped(manifest, settings.max_manifest_bytes, "manifest")
    try:
        parsed = parse_manifest(manifest_bytes)
        body = parsed.body
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

    # A product id is 64 bits of sha256("<product>:<board>"), so a collision is remote
    # (a million products, ~5e-8) -- but "remote" is not "impossible", and the id IS the
    # device's cross-flash guard: two product lines sharing one would offer each other's
    # firmware. Detect rather than trust the arithmetic. The account already has releases
    # for this id under a different name; the fix is an explicit product_id in the config.
    known = ms.product_manifest_name(product_id, account_id=account_id)
    if known is not None and body.get("product") and known != body["product"]:
        raise HTTPException(status_code=409,
                            detail="product_id %d already belongs to %r in this account; "
                                   "%r would collide. Set an explicit product_id in the "
                                   "project config." % (product_id, known, body["product"]))

    # A product-limited token publishes only into its own products. 404, not 403: the
    # error must not tell a limited credential which product ids exist.
    if not principal.may(product_id):
        raise HTTPException(status_code=404)

    latest = ms.latest_release_payload_version(product_id, account_id=account_id)
    if latest is not None and payload_version <= latest and not allow_republish:
        raise HTTPException(status_code=409, detail="payload_version %d <= latest %d "
                            "(pass allow_republish=true to override)" % (payload_version, latest))

    image_bytes = await _read_capped(image, settings.max_image_bytes, "image")
    # REPEATABLE. A release ships one delta per base version still in the field, because a
    # device patches against the release it is RUNNING -- one delta reaches only the devices
    # that never updated. Each is matched to its representation by filename.
    uploads = list(delta or [])
    deltas = {(u.filename or "").rsplit("/", 1)[-1]:
              await _read_capped(u, settings.max_image_bytes, "delta") for u in uploads}
    _verify_artifacts(body, image_bytes, deltas)

    # The SBOM rides beside the artifacts when the client sends one: the dependency evidence
    # for the exact bytes this release ships, served per release instead of living only on the
    # build machine. Validated as JSON only -- the render is the build's job, and a schema gate
    # here would reject evidence over formatting.
    sbom_bytes = (await _read_capped(sbom, settings.max_sbom_bytes, "sbom")
                  if sbom is not None else None)
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
                   key_id=parsed.key_id,   # which signing key vouches for these bytes
                   uploaded_by=principal.name, account_id=account_id,
                   dev=1 if body.get("dev") else 0,   # dev-signed provenance (visibility only)
                   sbom_key=sbom_key, display_name=display_name)
    ms.append_audit(actor=principal.name, action="release.publish", entity_type="release",
                    entity_id=release_id, data={"product_id": product_id, "version": body.get("version"),
                                                "payload_version": payload_version},
                    account_id=account_id, product_id=product_id)
    # CVE monitoring starts NOW, not at the next daily pass: scan the new release's
    # SBOM in the background (a scan failure never touches the publish result).
    if sbom_key is not None:
        def _scan_quietly(state=request.app.state,
                          rel={"release_id": release_id, "account_id": account_id,
                               "sbom_key": sbom_key}):
            from . import advisor
            try:
                advisor.scan_release(state, rel, actor=principal.name)
            except Exception as e:                            # noqa: BLE001
                print("publish-time advisory scan failed: %s" % e, file=sys.stderr)
        background.add_task(_scan_quietly)
    return {"release_id": release_id, "product_id": product_id,
            "product_id_str": str(product_id), "version": body.get("version"),
            "payload_version": payload_version, "representations": [r["format"] for r in reps],
            "display_name": display_name}

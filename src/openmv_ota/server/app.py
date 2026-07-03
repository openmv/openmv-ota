"""The FastAPI application factory + the device-facing API.

``create_app(settings, *, storage, metastore, verifier, admin_auth)`` is the embeddable core:
every collaborator is injectable (OpenMV's website supplies its own), defaulting to the
settings-driven backends for self-hosters. The device endpoints:

* ``POST /api/v1/check`` -- rate-limit (per IP) -> **registration verify** (unregistered leaves
  zero footprint) -> device-registry upsert -> rollout decision -> a capability manifest URL.
* ``GET /d/{token}/{filename}`` -- the capability gateway: validate the token, then 302-redirect
  to a presigned artifact URL (s3) or stream it (local). One token guards the whole bundle.
* ``GET /healthz``.

Routes live at module scope (not closed over ``create_app``) so FastAPI can resolve their type
hints under ``from __future__ import annotations``; per-request collaborators come off
``request.app.state``.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from . import capability
from .auth import TokenAuth
from .boardmap import swd_ids_board_code
from .errors import ServerError
from .metastore import build_metastore
from .ratelimit import RateLimiter
from .rollout import offers_update, should_autopause
from .storage import build_storage
from .verify import build_verifier

router = APIRouter()

_MEDIA = {"manifest.bin": "application/octet-stream"}


class CheckIn(BaseModel):
    device_id: str
    board_id: int                          # the release<->device join key (from identity())
    board: str | None = None               # camera model, display-only
    product: str | None = None
    app_version: str | None = None
    payload_version: int = 0
    slot: str | None = None
    representation: str | None = None
    fallback_reason: str | None = None
    confirmed: bool = False


def _media_type(filename: str) -> str:
    return _MEDIA.get(filename, "application/gzip")


def _artifact_key(release: dict, filename: str) -> str | None:
    """The storage key for the artifact named ``filename`` within ``release`` (or None)."""
    if filename == "manifest.bin":
        return release["manifest_key"]
    for rep in release["representations"]:
        if rep["url"] == filename:
            return release["image_key"] if rep["format"] == "full" else release["delta_key"]
    return None


def _decide(state, checkin, cohort):
    """The active rollout + release for this device, and whether to offer it.
    Returns ``(rollout | None, release | None, offered: bool, manifest_url | None)``."""
    ms = state.metastore
    ro = ms.active_rollout(checkin.board_id, cohort)
    if ro is None:
        return None, None, False, None
    rel = ms.get_release(ro["release_id"])
    if rel is None:
        return ro, None, False, None
    offered = offers_update(
        current_payload_version=checkin.payload_version,
        release_payload_version=rel["payload_version"], rollout_state=ro["state"],
        rollout_percent=ro["percent"], rollout_id=ro["rollout_id"], device_id=checkin.device_id)
    if not offered:
        return ro, rel, False, None
    token = capability.mint(state.secret, rel["release_id"], ttl=state.settings.capability_ttl)
    url = "%s/d/%s/manifest.bin" % (state.settings.base_url.rstrip("/"), token)
    return ro, rel, True, url


def _account(ms, ro, rel, checkin, existing, offered):
    """Feed the rollout's counters from this check-in + auto-pause on the failure threshold."""
    if ro is None or rel is None:
        return
    rid = ro["rollout_id"]
    prev_offered = existing["last_offered_release_id"] if existing else None
    prev_fallback = existing["fallback_reason"] if existing else None
    prev_pv = existing["current_payload_version"] if existing else None
    if offered and prev_offered != rel["release_id"]:            # newly entering this rollout
        ms.bump_rollout(rid, attempted=1)
    # a device we offered this release, now *transitioning to running it* -> a success
    if (prev_offered == rel["release_id"] and prev_pv != rel["payload_version"]
            and checkin.payload_version == rel["payload_version"]):
        ms.bump_rollout(rid, updated=1)
    # a device we offered this release, transitioning *into* a fallback -> one failure
    if prev_offered == rel["release_id"] and checkin.fallback_reason and not prev_fallback:
        ms.bump_rollout(rid, failures=1)
        fresh = ms.get_rollout(rid)
        if fresh["state"] == "active" and should_autopause(
                fresh["failures"], fresh["attempted"], fresh["failure_threshold"]):
            ms.update_rollout(rid, state="paused")
            ms.append_audit(actor="system", action="rollout.autopause", entity_type="rollout",
                            entity_id=rid,
                            data={"failures": fresh["failures"], "attempted": fresh["attempted"]})


@router.get("/healthz")
def healthz():
    return {"ok": True}


@router.post("/api/v1/check")
def check(checkin: CheckIn, request: Request):
    st = request.app.state
    nothing = {"update": False, "poll_after_s": st.settings.poll_after_s}
    ip = request.client.host if request.client else "-"
    if not st.ratelimit.allow(ip):
        return JSONResponse(nothing, status_code=429,
                            headers={"Retry-After": str(st.settings.poll_after_s)})
    # swd-ids matches on its own board codes (N6, H7), not firmware names (OPENMV_N6) -- translate.
    swd_board = swd_ids_board_code(checkin.board, st.settings.board_code_overrides)
    reg = st.verifier.verify(swd_board, checkin.device_id)
    if not reg.registered:
        return nothing                                          # ZERO footprint for unregistered ids
    ms = st.metastore
    existing = ms.get_device(checkin.device_id)
    cohort = existing["cohort"] if existing else "__default__"
    ro, rel, offered, manifest_url = _decide(st, checkin, cohort)
    _account(ms, ro, rel, checkin, existing, offered)
    release_id = rel["release_id"] if offered else None
    ms.upsert_device(
        device_id=checkin.device_id, board_id=checkin.board_id, board=checkin.board, cohort=cohort,
        current_version=checkin.app_version, current_payload_version=checkin.payload_version,
        slot=checkin.slot, representation=checkin.representation,
        fallback_reason=checkin.fallback_reason, confirmed=1 if checkin.confirmed else 0,
        last_offered_release_id=release_id, owner_ref=reg.owner_ref or None)
    if manifest_url:
        return {"update": True, "manifest_url": manifest_url, "release_id": release_id,
                "poll_after_s": st.settings.poll_after_s}
    return nothing


@router.get("/d/{token}/{filename}")
def artifact(token: str, filename: str, request: Request):
    st = request.app.state
    release_id = capability.verify(st.secret, token)
    if release_id is None:
        raise HTTPException(status_code=404)
    rel = st.metastore.get_release(release_id)
    key = _artifact_key(rel, filename) if rel is not None else None
    if key is None:
        raise HTTPException(status_code=404)
    url = st.storage.url_for(key)
    if url is not None:
        return RedirectResponse(url, status_code=302)           # offload to object storage
    try:
        data = st.storage.get(key)
    except ServerError:
        raise HTTPException(status_code=404) from None
    return Response(content=data, media_type=_media_type(filename))


def create_app(settings, *, storage=None, metastore=None, verifier=None, admin_auth=None):
    """Build the ASGI app. Collaborators default to the settings-driven backends; the website
    injects its own. The server HMAC secret comes from the DB (seeded by ``server init``) or
    ``OPENMV_OTA_COHORT_SALT`` -- required so capability tokens are stable across workers."""
    storage = storage if storage is not None else build_storage(settings)
    metastore = metastore if metastore is not None else build_metastore(settings)
    verifier = verifier if verifier is not None else build_verifier(settings)
    secret = metastore.get_meta("cohort_salt") or settings.cohort_salt
    if not secret:
        raise ServerError("no server secret -- run `server init` or set OPENMV_OTA_COHORT_SALT",
                          exit_code=2)

    app = FastAPI(title="openmv-ota update server", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.storage = storage
    app.state.metastore = metastore
    app.state.verifier = verifier
    app.state.admin_auth = admin_auth if admin_auth is not None else TokenAuth(metastore)
    app.state.secret = secret
    app.state.ratelimit = RateLimiter(settings.checkin_rate_per_min)
    app.include_router(router)
    from .admin import admin
    from .publish import publish
    app.include_router(admin)
    app.include_router(publish)
    return app

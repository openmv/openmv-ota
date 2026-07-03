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
    product_id: int                          # the release<->device join key (from identity())
    account_id: str = ""                     # the maker's account (from identity()); '' = self-host
    board: str | None = None               # camera model, display-only
    product: str | None = None
    app_version: str | None = None
    payload_version: int = 0
    slot: str | None = None
    representation: str | None = None
    fallback_reason: str | None = None
    confirmed: bool = False


class Feedback(BaseModel):
    device_id: str
    product_id: int
    account_id: str = ""                     # the maker's account (from identity()); '' = self-host
    board: str | None = None               # firmware board name (for the registration gate)
    release_id: str
    status: str                            # terminal outcome: 'installed' | 'failed'
    reason: str | None = None


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


def _offer(state, rel):
    """Mint a capability manifest URL for a release."""
    token = capability.mint(state.secret, rel["release_id"], ttl=state.settings.capability_ttl)
    return "%s/d/%s/manifest.bin" % (state.settings.base_url.rstrip("/"), token)


def _decide(state, checkin, cohort, existing=None, account_id=""):
    """The release to offer this device (a pin overrides the rollout) and whether to offer it.
    ``account_id`` is the device's *effective* account (its sticky binding, not the raw report).
    Returns ``(rollout | None, release | None, offered: bool, manifest_url | None)``."""
    ms = state.metastore
    # A pin (device wins over cohort) overrides the rollout: offer the pinned release iff it's an
    # upgrade -- a pin to the current/older version just holds the device (no rollout reaches it).
    pinned = (existing["pinned_release_id"] if existing else None) \
        or ms.get_cohort_pin(checkin.product_id, cohort, account_id=account_id)
    if pinned:
        rel = ms.get_release(pinned)
        # a release is only ever offered to a device of its own account (defense in depth behind
        # the admin-side pin check): a cross-account or missing/older pin just holds the device.
        if (rel is None or rel["account_id"] != account_id
                or rel["payload_version"] <= checkin.payload_version):
            return None, rel, False, None
        return None, rel, True, _offer(state, rel)
    ro = ms.active_rollout(checkin.product_id, cohort, account_id=account_id)
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
    return ro, rel, True, _offer(state, rel)


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
                            entity_id=rid, account_id=ro.get("account_id", ""),
                            data={"failures": fresh["failures"], "attempted": fresh["attempted"]})


def _verify(state, req):
    """Translate the firmware board name to the swd-ids code, then the registration check.
    ``req`` is a CheckIn or Feedback (both carry ``board`` + ``device_id``)."""
    swd_board = swd_ids_board_code(req.board, state.settings.board_code_overrides)
    return state.verifier.verify(swd_board, req.device_id)


def _effective_account(ms, checkin):
    """The device's authoritative account for scoping. A binding (learned on the first valid
    check-in, or an admin override) is **sticky** and wins -- so a later golden fallback reporting
    a different/empty account can't strand the device. Absent a binding, the first non-empty report
    is *learned* (and returned); a device that has only ever reported '' stays in the '' account.
    Only reached for a registered device, so no unregistered id can create a binding."""
    bound = ms.device_account(checkin.device_id)
    if bound is not None:
        return bound["account_id"]
    if checkin.account_id:
        ms.bind_device_account(checkin.device_id, checkin.account_id, source="learned")
        return checkin.account_id
    return ""


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

    if checkin.board in st.settings.unverified_boards:
        # A board type swd-ids never registers (legacy Arduino, pre-registration M4). Serve OTA
        # READ-ONLY: skip the registration check AND the device-registry write, so a fake id can't
        # grow the DB -- zero footprint preserved, at the cost of no fleet tracking for these boards.
        _, rel, offered, manifest_url = _decide(st, checkin, "__default__")
        if manifest_url:
            return {"update": True, "manifest_url": manifest_url,
                    "release_id": rel["release_id"], "poll_after_s": st.settings.poll_after_s}
        return nothing

    reg = _verify(st, checkin)
    if not reg.registered:
        return nothing                                          # ZERO footprint for unregistered ids
    ms = st.metastore
    account_id = _effective_account(ms, checkin)                # sticky binding, not the raw report
    existing = ms.get_device(checkin.device_id)
    cohort = existing["cohort"] if existing else "__default__"
    ro, rel, offered, manifest_url = _decide(st, checkin, cohort, existing, account_id)
    _account(ms, ro, rel, checkin, existing, offered)
    release_id = rel["release_id"] if offered else None
    ms.upsert_device(
        device_id=checkin.device_id, product_id=checkin.product_id, board=checkin.board, cohort=cohort,
        current_version=checkin.app_version, current_payload_version=checkin.payload_version,
        slot=checkin.slot, representation=checkin.representation,
        fallback_reason=checkin.fallback_reason, confirmed=1 if checkin.confirmed else 0,
        last_offered_release_id=release_id, registrar_ref=reg.registrar_ref or None,
        account_id=account_id)
    if manifest_url:
        return {"update": True, "manifest_url": manifest_url, "release_id": release_id,
                "poll_after_s": st.settings.poll_after_s}
    return nothing


@router.post("/api/v1/feedback")
def feedback(report: Feedback, request: Request):
    """Explicit terminal outcome of an offered update (precise success/failure vs. inferring it
    from the next check-in). Recorded ONLY for a registered device -- an unregistered or bypassed
    board is a no-op, so this stays zero-footprint too."""
    st = request.app.state
    ip = request.client.host if request.client else "-"
    if not st.ratelimit.allow(ip):
        return JSONResponse({"ok": False}, status_code=429,
                            headers={"Retry-After": str(st.settings.poll_after_s)})
    if report.status not in ("installed", "failed"):
        raise HTTPException(status_code=400, detail="status must be 'installed' or 'failed'")
    if report.board in st.settings.unverified_boards or not _verify(st, report).registered:
        return {"ok": False}                                    # untracked / unregistered -> no write
    st.metastore.record_deployment(
        device_id=report.device_id, release_id=report.release_id, product_id=report.product_id,
        status=report.status, reason=report.reason, account_id=report.account_id)
    return {"ok": True}


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

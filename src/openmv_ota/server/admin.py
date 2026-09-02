"""The admin API -- rollouts + fleet observability. Token+scope-authed; every mutation audited.

(Release *publish* is in ``publish.py``; it needs the artifact codec.) Handlers read the metastore
off ``request.app.state`` and gate on a scope via ``require_scope``.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from . import live as live_mod
from .auth import Principal, hash_token, require_scope
from .schemas import (
    AccountActive,
    AccountCreated,
    AccountList,
    AccountNamed,
    ArtifactsDeleted,
    AuditList,
    CohortAssigned,
    CohortList,
    CohortPinned,
    CohortDeleted,
    CohortRenamed,
    Device,
    DeviceBound,
    DeviceList,
    DevicePinned,
    FleetBases,
    FleetSummary,
    Release,
    ReleaseList,
    Rollout,
    RolloutCreated,
    RolloutList,
    RolloutState,
    RolloutStatus,
    TokenIssued,
    TokenList,
    TokenRevoked,
    ViewerGrant,
)
from .scopes import ALL_SCOPES, SCOPES

admin = APIRouter(prefix="/api/v1/admin")


# Default page size for the paginated admin lists. `/devices` already capped at 100; `/releases`
# and `/rollouts` defaulted to NO limit, so a fleet with thousands of releases returned all of
# them in one response. Same number everywhere is the point -- a caller should not have to
# remember which collection happens to be unbounded.
_PAGE = 100


def new_id(prefix: str) -> str:
    return "%s_%s" % (prefix, secrets.token_hex(8))


def _owned(entity, principal):
    """Return ``entity`` iff it belongs to the caller's account; else 404 -- a missing entity and
    another account's entity are indistinguishable, so cross-account probing leaks nothing."""
    if entity is None or entity.get("account_id", "") != principal.account_id:
        raise HTTPException(status_code=404)
    return entity


class RolloutCreate(BaseModel):
    release_id: str
    cohort: str = "__default__"
    percent: float
    failure_threshold: float = 0.05


class RolloutPatch(BaseModel):
    percent: float | None = None
    state: str | None = None


class CohortAssign(BaseModel):
    cohort: str
    device_ids: list[str] | None = None    # surgical: these exact devices
    product_id: int | None = None          # bulk: every device of this product


class CohortRename(BaseModel):
    cohort: str                            # the label to rename
    name: str                              # the new label


class DevicePin(BaseModel):
    release_id: str | None = None          # null unpins


class CohortPin(BaseModel):
    product_id: int
    cohort: str
    release_id: str | None = None          # null unpins (the account comes from the caller's token)


class AccountCreate(BaseModel):
    name: str


def _clean_name(ms, name, except_id=None):
    """A non-empty, unique (case-insensitive) account name, or an HTTPException (400 empty / 409
    taken). Shared by create + rename so both enforce the same rule."""
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="account name must not be empty")
    if ms.account_name_exists(name, except_id):
        raise HTTPException(status_code=409, detail="an account named %r already exists" % name)
    return name


@admin.post("/accounts", responses={200: {"model": AccountCreated}})
def create_account(body: AccountCreate, request: Request,
                   principal: Principal = Depends(require_scope("accounts"))):
    """Operator-only (``accounts``): create a tenant account + issue its first admin token.
    The remote equivalent of ``server account create``; the website (or a self-host super-admin)
    drives it. The token is returned once and only its hash is stored."""
    ms = request.app.state.metastore
    name = _clean_name(ms, body.name)
    account_id = "acct_" + secrets.token_hex(8)
    token = secrets.token_urlsafe(32)
    ms.add_account(account_id, name)
    ms.add_token(hash_token(token), name, list(SCOPES), account_id=account_id)
    ms.append_audit(actor=principal.name, action="account.create", entity_type="account",
                    entity_id=account_id, data={"name": name},
                    account_id=principal.account_id)
    return {"account_id": account_id, "name": name, "token": token}


@admin.get("/accounts", responses={200: {"model": AccountList}})
def list_accounts(request: Request,
                  principal: Principal = Depends(require_scope("accounts"))):
    return {"accounts": request.app.state.metastore.list_accounts()}


class AccountPatch(BaseModel):
    name: str


@admin.patch("/accounts/{account_id}", responses={200: {"model": AccountNamed}})
def patch_account(account_id: str, body: AccountPatch, request: Request,
                  principal: Principal = Depends(require_scope("accounts"))):
    ms = request.app.state.metastore
    if ms.get_account(account_id) is None:
        raise HTTPException(status_code=404)
    name = _clean_name(ms, body.name, except_id=account_id)
    ms.rename_account(account_id, name)
    ms.append_audit(actor=principal.name, action="account.rename", entity_type="account",
                    entity_id=account_id, data={"name": name}, account_id=principal.account_id)
    return {"account_id": account_id, "name": name}


@admin.post("/accounts/{account_id}/deactivate", responses={200: {"model": AccountActive}})
def deactivate_account(account_id: str, request: Request,
                       principal: Principal = Depends(require_scope("accounts"))):
    """Soft off-switch: revoke every token + set active=0. Admin access dies; fielded devices keep
    being served (a billing lapse doesn't brick a fleet), and no new token can be minted until the
    account is reactivated."""
    ms = request.app.state.metastore
    if ms.get_account(account_id) is None:
        raise HTTPException(status_code=404)
    n = ms.revoke_account_tokens(account_id)
    ms.set_account_active(account_id, False)
    ms.append_audit(actor=principal.name, action="account.deactivate", entity_type="account",
                    entity_id=account_id, data={"tokens_revoked": n}, account_id=principal.account_id)
    return {"account_id": account_id, "active": False, "tokens_revoked": n}


@admin.post("/accounts/{account_id}/activate", responses={200: {"model": AccountActive}})
def activate_account(account_id: str, request: Request,
                     principal: Principal = Depends(require_scope("accounts"))):
    """Re-enable an account (active=1). Does NOT un-revoke old tokens -- issue fresh ones."""
    ms = request.app.state.metastore
    if ms.get_account(account_id) is None:
        raise HTTPException(status_code=404)
    ms.set_account_active(account_id, True)
    ms.append_audit(actor=principal.name, action="account.activate", entity_type="account",
                    entity_id=account_id, account_id=principal.account_id)
    return {"account_id": account_id, "active": True}


# --- token management (operator-only: 'accounts' scope) ------------------------------------
# Deliberately NOT reachable by a normal worker token -- so a stolen publish/manage/observe token
# can't mint a second, revocation-surviving token. A token secret is returned ONLY here (issue /
# rotate), never in a list/get; the store keeps only the hash. token_hash is the non-secret id.

class TokenIssue(BaseModel):
    name: str
    scopes: list[str] | None = None        # default: the worker set (publish/manage/observe)


def _mint(ms, principal, name, scopes, account_id, action, extra=None):
    token = secrets.token_urlsafe(32)
    th = hash_token(token)
    ms.add_token(th, name, scopes, account_id=account_id)
    ms.append_audit(actor=principal.name, action=action, entity_type="token", entity_id=th,
                    data={"account_id": account_id, "name": name, **(extra or {})},
                    account_id=principal.account_id)
    return {"token_hash": th, "name": name, "scopes": scopes, "account_id": account_id, "token": token}


def _active_account(ms, account_id):
    """The account, requiring it to exist (404) and be active (409). Gate for minting tokens --
    a deactivated account must never get a fresh working credential (issue *or* rotate)."""
    acc = ms.get_account(account_id)
    if acc is None:
        raise HTTPException(status_code=404)
    if not acc["active"]:
        raise HTTPException(status_code=409, detail="account is deactivated")
    return acc


@admin.post("/accounts/{account_id}/tokens", responses={200: {"model": TokenIssued}})
def issue_token(account_id: str, body: TokenIssue, request: Request,
                principal: Principal = Depends(require_scope("accounts"))):
    ms = request.app.state.metastore
    _active_account(ms, account_id)                            # 404 missing / 409 deactivated
    scopes = body.scopes if body.scopes is not None else list(SCOPES)
    bad = [s for s in scopes if s not in ALL_SCOPES]
    if bad:
        raise HTTPException(status_code=400, detail="unknown scope(s): %s" % ", ".join(bad))
    return _mint(ms, principal, body.name, scopes, account_id, "token.issue")


@admin.get("/accounts/{account_id}/tokens", responses={200: {"model": TokenList}})
def list_account_tokens(account_id: str, request: Request,
                        principal: Principal = Depends(require_scope("accounts"))):
    ms = request.app.state.metastore
    if ms.get_account(account_id) is None:
        raise HTTPException(status_code=404)
    return {"tokens": ms.list_tokens(account_id=account_id)}   # metadata only -- never the secret


@admin.post("/tokens/{token_hash}/revoke", responses={200: {"model": TokenRevoked}})
def revoke_token(token_hash: str, request: Request,
                 principal: Principal = Depends(require_scope("accounts"))):
    ms = request.app.state.metastore
    if ms.get_token(token_hash) is None:
        raise HTTPException(status_code=404)
    ms.revoke_token(token_hash)
    ms.append_audit(actor=principal.name, action="token.revoke", entity_type="token",
                    entity_id=token_hash, account_id=principal.account_id)
    return {"token_hash": token_hash, "revoked": True}


@admin.post("/tokens/{token_hash}/rotate", responses={200: {"model": TokenIssued}})
def rotate_token(token_hash: str, request: Request,
                 principal: Principal = Depends(require_scope("accounts"))):
    """Issue a replacement (same name/scopes/account) and revoke the old one -- the recovery path
    for a lost/leaked token. Returns the new secret once."""
    ms = request.app.state.metastore
    old = ms.get_token(token_hash)
    if old is None:
        raise HTTPException(status_code=404)
    _active_account(ms, old["account_id"])                     # can't rotate into a deactivated account
    fresh = _mint(ms, principal, old["name"], old["scopes"], old["account_id"], "token.rotate",
                  extra={"replaced": token_hash})
    ms.revoke_token(token_hash)
    return fresh


@admin.post("/rollouts", responses={200: {"model": RolloutCreated}})
def create_rollout(body: RolloutCreate, request: Request,
                   principal: Principal = Depends(require_scope("manage"))):
    ms = request.app.state.metastore
    rel = _owned(ms.get_release(body.release_id), principal)   # 404 if missing or another account's
    product_id = rel["product_id"]
    account_id = principal.account_id                      # the rollout inherits the caller's account
    prior = ms.active_rollout(product_id, body.cohort, account_id=account_id)   # one active per (account, product, cohort)
    if prior is not None:
        ms.update_rollout(prior["rollout_id"], state="paused")
        ms.append_audit(actor=principal.name, action="rollout.superseded", entity_type="rollout",
                        entity_id=prior["rollout_id"], account_id=account_id)
    rid = new_id("ro")
    ms.add_rollout(rollout_id=rid, release_id=body.release_id, product_id=product_id,
                   cohort=body.cohort, percent=body.percent,
                   failure_threshold=body.failure_threshold, account_id=account_id)
    ms.append_audit(actor=principal.name, action="rollout.create", entity_type="rollout",
                    entity_id=rid, data={"release_id": body.release_id, "cohort": body.cohort,
                                         "percent": body.percent}, account_id=account_id)
    return {"rollout_id": rid, "product_id": product_id, "cohort": body.cohort,
            "percent": body.percent, "state": "active"}


@admin.patch("/rollouts/{rollout_id}", responses={200: {"model": Rollout}})
def patch_rollout(rollout_id: str, body: RolloutPatch, request: Request,
                  principal: Principal = Depends(require_scope("manage"))):
    ms = request.app.state.metastore
    ro = _owned(ms.get_rollout(rollout_id), principal)
    changes: dict = {}
    if body.percent is not None:
        if body.percent < ro["percent"]:
            raise HTTPException(status_code=400, detail="percent is monotonic (can only rise)")
        changes["percent"] = body.percent
    if body.state is not None:
        if body.state not in ("active", "paused"):
            raise HTTPException(status_code=400, detail="state must be active or paused")
        if ro["state"] == "stopped":
            # stop is TERMINAL -- the docs promise it, and a resume here would silently
            # re-offer a release the operator decided nobody else should get
            raise HTTPException(status_code=409, detail="rollout is stopped -- create a new one")
        changes["state"] = body.state
    if not changes:
        raise HTTPException(status_code=400, detail="nothing to change")
    ms.update_rollout(rollout_id, **changes)
    ms.append_audit(actor=principal.name, action="rollout.update", entity_type="rollout",
                    entity_id=rollout_id, data=changes, account_id=principal.account_id)
    return ms.get_rollout(rollout_id)


@admin.post("/rollouts/{rollout_id}/stop", responses={200: {"model": RolloutState}})
def stop_rollout(rollout_id: str, request: Request,
                     principal: Principal = Depends(require_scope("manage"))):
    ms = request.app.state.metastore
    _owned(ms.get_rollout(rollout_id), principal)
    ms.update_rollout(rollout_id, state="stopped")       # stops offering; does not downgrade
    ms.append_audit(actor=principal.name, action="rollout.stop", entity_type="rollout",
                    entity_id=rollout_id, account_id=principal.account_id)
    return {"rollout_id": rollout_id, "state": "stopped"}


# The list is pure ENUMERATION -- enough to find and recognize a rollout -- while
# /status is the complete single-rollout read (identity, policy, timestamps, counters,
# derived score). Everything specific to one rollout lives there, once.
_ROLLOUT_ROW = ("rollout_id", "release_id", "product_id", "cohort", "percent", "state",
                "cohort_devices")


@admin.get("/rollouts", responses={200: {"model": RolloutList}})
def list_rollouts(request: Request, product_id: int | None = None, limit: int = _PAGE,
                  offset: int = 0, state: str | None = None,
                  principal: Principal = Depends(require_scope("observe"))):
    ms = request.app.state.metastore
    rows = ms.list_rollouts(product_id, account_id=principal.account_id,
                            limit=limit, offset=offset, state=state)
    return {"rollouts": [{k: r[k] for k in _ROLLOUT_ROW} for r in rows],
            "total": ms.count_scoped("rollouts", product_id, principal.account_id)}


@admin.get("/rollouts/{rollout_id}/status", responses={200: {"model": RolloutStatus}})
def rollout_status(rollout_id: str, request: Request,
                   principal: Principal = Depends(require_scope("observe"))):
    ms = request.app.state.metastore
    ro = _owned(ms.get_rollout(rollout_id), principal)
    cohort_devices = ms.cohort_device_count(ro["product_id"], ro["cohort"],
                                            ro.get("account_id", ""))
    # The current target: percent of the audience. An ESTIMATE -- membership is a hash,
    # not a list, so the true staged count varies around it (and shifts as the cohort does).
    staged = round(cohort_devices * ro["percent"] / 100)
    rates = ({k: ro[k] / staged for k in ("attempted", "updated", "failures")}
             if staged else None)
    # the COMPLETE single-rollout read: the stored row (identity, policy, timestamps,
    # counters), plus the audience and the derived score
    return {**ro, "cohort_devices": cohort_devices, "staged_devices": staged,
            # each counter as a fraction of staged_devices -- how far through the current
            # target each metric is; null until anything is staged
            "rates": rates,
            # explicit device reports (POST /feedback) for this rollout's release
            "reported": ms.deployment_counts(ro["release_id"])}


@admin.get("/cohorts", responses={200: {"model": CohortList}})
def list_cohorts(request: Request, product_id: int | None = None,
                 principal: Principal = Depends(require_scope("observe"))):
    return {"cohorts": request.app.state.metastore.list_cohorts(
        product_id, account_id=principal.account_id)}


@admin.post("/cohorts/assign", responses={200: {"model": CohortAssigned}})
def assign_cohort(body: CohortAssign, request: Request,
                  principal: Principal = Depends(require_scope("manage"))):
    """Move devices into a cohort -- surgically by id, or in bulk by product (exactly one
    selector). Both are scoped to the caller's account: an id (or a product's device)
    belonging to another account is silently skipped, never revealed."""
    ms = request.app.state.metastore
    if (body.device_ids is None) == (body.product_id is None):
        raise HTTPException(status_code=400,
                            detail="pass exactly one of device_ids or product_id")
    if body.device_ids is not None:
        n = ms.assign_cohort(body.device_ids, body.cohort, account_id=principal.account_id)
        data = {"assigned": n, "requested": len(body.device_ids)}
    else:
        n = ms.assign_cohort_product(body.product_id, body.cohort,
                                     account_id=principal.account_id)
        data = {"assigned": n, "product_id": body.product_id}
    ms.append_audit(actor=principal.name, action="cohort.assign", entity_type="cohort",
                    entity_id=body.cohort, data=data, account_id=principal.account_id)
    return {"cohort": body.cohort, "assigned": n}


@admin.post("/cohorts/rename", responses={200: {"model": CohortRenamed}})
def rename_cohort(body: CohortRename, request: Request,
                  principal: Principal = Depends(require_scope("manage"))):
    """Relabel a cohort everywhere at once -- device rows, rollouts, pins -- so nothing
    orphans: a rollout keeps reaching exactly the devices it did (staging hashes on ids,
    never the name). ``__default__`` is refused on either side (it is where new devices
    arrive, not a label you own), and a target already in use is refused too: merging
    two cohorts is an explicit `assign`, never a rename surprise."""
    ms = request.app.state.metastore
    old, new = (body.cohort or "").strip(), (body.name or "").strip()
    if not old or not new or old == new:
        raise HTTPException(status_code=400, detail="need two different, non-empty names")
    if "__default__" in (old, new):
        raise HTTPException(status_code=400, detail="__default__ cannot be renamed or targeted")
    if ms.cohort_in_use(new, account_id=principal.account_id):
        raise HTTPException(status_code=409,
                            detail="cohort %r is already in use -- merging is `assign`, "
                                   "not rename" % new)
    counts = ms.rename_cohort(old, new, account_id=principal.account_id)
    ms.append_audit(actor=principal.name, action="cohort.rename", entity_type="cohort",
                    entity_id=old, data={"to": new, **counts}, account_id=principal.account_id)
    return {"cohort": new, "renamed_from": old, **counts}


class CohortDelete(BaseModel):
    cohort: str


@admin.post("/cohorts/delete", responses={200: {"model": CohortDeleted}})
def delete_cohort(body: CohortDelete, request: Request,
                  principal: Principal = Depends(require_scope("manage"))):
    """Retire a label: its devices return to ``__default__`` and its pins drop.
    ``__default__`` itself is refused, and so is a cohort an **active** rollout still
    targets (pause or stop it first) -- deleting the audience out from under a live
    rollout would silently strand it. Paused/stopped rollout rows keep the old name:
    they are history."""
    ms = request.app.state.metastore
    cohort = (body.cohort or "").strip()
    if not cohort or cohort == "__default__":
        raise HTTPException(status_code=400, detail="__default__ cannot be deleted")
    if ms.cohort_has_active_rollout(cohort, account_id=principal.account_id):
        raise HTTPException(status_code=409,
                            detail="an active rollout targets cohort %r -- pause or stop "
                                   "it first" % cohort)
    counts = ms.delete_cohort(cohort, account_id=principal.account_id)
    ms.append_audit(actor=principal.name, action="cohort.delete", entity_type="cohort",
                    entity_id=cohort, data=counts, account_id=principal.account_id)
    return {"cohort": cohort, **counts}


def _check_pin_release(ms, release_id, principal):
    """If the pin targets an *existing* release, it must belong to the caller's account (else the
    device could be handed another account's signed bytes). A None/not-yet-published release_id is
    allowed -- the device path simply holds until such a release exists (and the device-path guard
    re-checks the account when it does)."""
    if release_id is not None:
        rel = ms.get_release(release_id)
        if rel is not None and rel.get("account_id", "") != principal.account_id:
            raise HTTPException(status_code=404)


@admin.patch("/devices/{device_id}/pin", responses={200: {"model": DevicePinned}})
def pin_device(device_id: str, body: DevicePin, request: Request,
               principal: Principal = Depends(require_scope("manage"))):
    ms = request.app.state.metastore
    _owned(ms.get_device(device_id), principal)              # 404 if missing or another account's
    _check_pin_release(ms, body.release_id, principal)
    ms.set_device_pin(device_id, body.release_id)            # release_id=None unpins
    ms.append_audit(actor=principal.name, action="device.pin", entity_type="device",
                    entity_id=device_id, data={"release_id": body.release_id},
                    account_id=principal.account_id)
    return {"device_id": device_id, "pinned_release_id": body.release_id}


@admin.post("/devices/{device_id}/account", responses={200: {"model": DeviceBound}})
def bind_device(device_id: str, request: Request,
                principal: Principal = Depends(require_scope("manage"))):
    """Operator override: (re)bind a device to the caller's account -- the authority for
    re-accounting a device or recovering one wrongly *learned* onto another account (which the
    signature already stops from installing anything). A device already *admin*-bound to a different
    account is 404 (not yours; no existence leak), so one account can't steal another's binding via
    the API. On a shared server, gate who may call this by proof of ownership (see
    docs/compliance/residual-threats.md)."""
    ms = request.app.state.metastore
    cur = ms.device_account(device_id)
    if cur is not None and cur["source"] == "admin" and cur["account_id"] != principal.account_id:
        raise HTTPException(status_code=404)
    ms.bind_device_account(device_id, principal.account_id, source="admin")
    ms.set_device_account(device_id, principal.account_id)   # sync the row so fleet views update now
    ms.append_audit(actor=principal.name, action="device.bind", entity_type="device",
                    entity_id=device_id, data={"account_id": principal.account_id},
                    account_id=principal.account_id)
    return {"device_id": device_id, "account_id": principal.account_id}


@admin.post("/cohorts/pin", responses={200: {"model": CohortPinned}})
def pin_cohort(body: CohortPin, request: Request,
               principal: Principal = Depends(require_scope("manage"))):
    ms = request.app.state.metastore
    _check_pin_release(ms, body.release_id, principal)
    ms.set_cohort_pin(body.product_id, body.cohort, body.release_id,
                      account_id=principal.account_id)       # account from the token, not the body
    ms.append_audit(actor=principal.name, action="cohort.pin", entity_type="cohort",
                    entity_id=body.cohort, data={"product_id": body.product_id,
                                                 "release_id": body.release_id},
                    account_id=principal.account_id)
    return {"product_id": body.product_id, "cohort": body.cohort, "release_id": body.release_id}


@admin.get("/fleet", responses={200: {"model": FleetSummary}})
def fleet(request: Request, product_id: int | None = None, cohort: str | None = None,
          principal: Principal = Depends(require_scope("observe"))):
    from openmv_ota.ota.version import decode_app_version

    summary = request.app.state.metastore.fleet_summary(product_id,
                                                        account_id=principal.account_id,
                                                        cohort=cohort)
    # by_fallback is keyed by the packed uint32 the device reports; render it the way
    # by_version already reads. "unknown" is the device that did not say -- a single-image
    # board, or one on a payload from before the slots field existed.
    for prod in summary["products"].values():
        prod["by_fallback"] = {
            (decode_app_version(k) if k else "unknown"): n
            for k, n in prod["by_fallback"].items()}
    return summary


@admin.get("/fleet/bases", responses={200: {"model": FleetBases}})
def fleet_bases(request: Request, product_id: int | None = None,
                principal: Principal = Depends(require_scope("observe"))):
    """The distinct (version, body_sha256) bases the fleet is RUNNING, with device counts --
    the release-planning answer to "which delta bases must this release cover?". Grouped by
    exact bytes: two rows for one version means a republish split the fleet, and only the
    row matching the store's bytes can take a delta (`client release bases --fleet` reads
    exactly this and warns about the rest)."""
    from openmv_ota.ota.version import decode_app_version

    rows = request.app.state.metastore.fleet_bases(product_id,
                                                   account_id=principal.account_id)
    for r in rows:
        r["version"] = decode_app_version(r["payload_version"])
    return {"bases": rows}


@admin.get("/releases", responses={200: {"model": ReleaseList}})
def releases(request: Request, product_id: int | None = None, limit: int = _PAGE,
             offset: int = 0, principal: Principal = Depends(require_scope("observe"))):
    ms = request.app.state.metastore
    return {"releases": ms.list_releases(product_id, account_id=principal.account_id,
                                         limit=limit, offset=offset),
            "total": ms.count_scoped("releases", product_id, principal.account_id)}


def _with_fallback_version(rows: list[dict]) -> list[dict]:
    """Add a human-readable ``fallback_version`` beside the stored uint32.

    The store keeps the packed number because that is what the device reports and what
    comparisons need; a reader should not have to decode `16711680` in their head to answer
    "what would this device fall back to". Absent when the device did not tell us, which is
    deliberately distinct from a device that reported no fallback."""
    from openmv_ota.ota.version import decode_app_version

    for row in rows:
        packed = row.get("fallback_payload_version")
        row["fallback_version"] = decode_app_version(packed) if packed else None
    return rows


@admin.get("/releases/{release_id}", responses={200: {"model": Release}})
def release(release_id: str, request: Request,
            principal: Principal = Depends(require_scope("observe"))):
    """One release. A UI's release page had no way to ask for a single release -- only to LIST and
    filter client-side, which means paging until the row turns up on any fleet with real history.
    Ownership is checked the same way as everywhere else, so another account's release is a 404 and
    not a probe."""
    return _owned(request.app.state.metastore.get_release(release_id), principal)


@admin.get("/devices/{device_id}", responses={200: {"model": Device}})
def device(device_id: str, request: Request,
           principal: Principal = Depends(require_scope("observe"))):
    """One device, shaped exactly like a row of ``GET /devices`` (same ``fallback_version``
    decoding) so a UI can render a list row and a detail page from one model."""
    row = _owned(request.app.state.metastore.get_device(device_id), principal)
    return _with_fallback_version([row])[0]


@admin.get("/releases/{release_id}/image", responses={200: {"content": {"application/gzip": {}}, "description": "the artifact bytes"}})
def release_image(release_id: str, request: Request,
                  principal: Principal = Depends(require_scope("observe"))):
    """Download a retained release's image -- the bytes needed to build a delta FROM it.

    The server keeps every published image, and this is what that retention is for. A delta
    must be named in the SIGNED manifest, and the server never holds signing keys, so it can
    never generate one itself: the maker builds deltas locally and therefore needs the older
    images. Serving them back means a build machine does not have to hoard artifacts for every
    version still in the field -- lose the directory, re-clone the repo, or hand the release
    to a colleague, and the bases are still there.

    Account-scoped like every other release read: another account's release is a 404, not a
    403, so this cannot be used to probe for release ids."""
    from .errors import ServerError

    st = request.app.state
    rel = _owned(st.metastore.get_release(release_id), principal)
    try:
        data = st.storage.get(rel["image_key"])
    except ServerError:
        # The row survives its bytes: a storage lifecycle rule, a bucket migration, or a
        # retention tier that has expired. Say so plainly -- "the release exists but its image
        # is gone" is a different problem for the caller than "no such release".
        raise HTTPException(status_code=404,
                            detail="image is no longer retained") from None
    return Response(content=data, media_type="application/gzip")


@admin.get("/releases/{release_id}/sbom", responses={200: {"content": {"application/json": {}}, "description": "the release's CycloneDX SBOM"}})
def release_sbom(release_id: str, request: Request,
                 principal: Principal = Depends(require_scope("observe"))):
    """The release's SBOM (CycloneDX JSON), as uploaded at publish -- the dependency evidence
    for the exact bytes this release ships. 404 when the release was published without one
    (an older client) or the object is no longer retained. Account-scoped like every other
    release read."""
    from .errors import ServerError

    st = request.app.state
    rel = _owned(st.metastore.get_release(release_id), principal)
    if not rel.get("sbom_key"):
        raise HTTPException(status_code=404, detail="release has no SBOM")
    try:
        data = st.storage.get(rel["sbom_key"])
    except ServerError:
        raise HTTPException(status_code=404, detail="sbom is no longer retained") from None
    return Response(content=data, media_type="application/json")


@admin.delete("/releases/{release_id}/artifacts", responses={200: {"model": ArtifactsDeleted}})
def delete_release_artifacts(release_id: str, request: Request, force: bool = False,
                             principal: Principal = Depends(require_scope("publish"))):
    """Delete a release's stored objects, keeping the release ROW.

    Retention has no depth limit -- images are small and cheap to keep, and a delta base is
    only useful for as long as devices are still running that version, which only the operator
    knows. So reclaiming space is a deliberate act, and this is it.

    The row survives on purpose. It is the audit trail and the anti-rollback history, and
    `GET /releases/{id}/image` already answers "image is no longer retained" for exactly this
    state -- a release that existed, whose bytes are gone -- which a caller must be able to
    tell apart from a release that never existed.

    REFUSED while a rollout still points at the release, because those are the devices being
    offered it right now: deleting the image mid-rollout turns every in-flight download into a
    404. ``force`` is there for the case the operator means it (a rolled-back release nobody
    should install), and it says so rather than silently allowing it."""
    st = request.app.state
    ms = st.metastore
    rel = _owned(ms.get_release(release_id), principal)
    live = [r for r in ms.rollouts_for_release(release_id, account_id=principal.account_id)
            if r["state"] == "active"]
    if live and not force:
        raise HTTPException(
            status_code=409,
            detail="release %s is still being offered by rollout(s) %s -- pause or stop them "
                   "first, or pass force=true"
                   % (release_id, ", ".join(r["rollout_id"] for r in live)))

    keys = [rel["manifest_key"], rel["image_key"]]
    keys += ["artifacts/%s/%s" % (release_id, rep["url"].rsplit("/", 1)[-1])
             for rep in rel["representations"] if rep["format"] != "full"]
    # Report what was actually REMOVED, not what was attempted: `delete` is idempotent on both
    # backends (missing_ok / delete_object), so a second call would otherwise claim to have
    # deleted the same objects again and an operator could not tell whether anything was there.
    deleted = []
    for key in keys:
        if not st.storage.exists(key):
            continue
        st.storage.delete(key)
        deleted.append(key)
    ms.append_audit(actor=principal.name, action="release.artifacts.delete",
                    entity_type="release", entity_id=release_id,
                    data={"deleted": len(deleted), "forced": bool(force)},
                    account_id=principal.account_id)
    return {"release_id": release_id, "deleted": deleted}


@admin.get("/devices", responses={200: {"model": DeviceList}})
def devices(request: Request, product_id: int | None = None, limit: int = 100,
            cohort: str | None = None, offset: int = 0,
            principal: Principal = Depends(require_scope("observe"))):
    ms = request.app.state.metastore
    return {"devices": _with_fallback_version(ms.list_devices(
                product_id, limit, account_id=principal.account_id, cohort=cohort, offset=offset)),
            # `total` ignores the cohort filter only when one is not given; with one it still
            # counts the scoped set, so a cohort page reports the fleet total. Documented rather
            # than silently wrong: cohort sizes come from GET /cohorts, which counts per cohort.
            "total": ms.count_scoped("devices", product_id, principal.account_id)}


@admin.post("/devices/{device_id}/viewer-grant", responses={200: {"model": ViewerGrant}})
def viewer_grant(device_id: str, request: Request,
                 principal: Principal = Depends(require_scope("observe"))):
    """Mint a short-lived, single-device ``viewer`` credential for a dashboard.

    This is the issuer for the read side: the relay and the datalake both refuse
    anything without a viewer token, and the signing secret lives only here. A
    dashboard backend authenticates its own user however it likes, then calls
    this with its account's ``observe`` token to get a credential it can hand to
    that user's browser.

    Ownership comes from the sticky device->account binding, not from whatever
    the device last claimed, and an unowned device is a 404 like any other
    entity -- so this cannot be used to discover other accounts' devices."""
    st = request.app.state
    ms = st.metastore
    device = ms.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404)
    bound = ms.device_account(device_id)          # the sticky binding wins
    owner = bound["account_id"] if bound else device.get("account_id", "")
    _owned({"account_id": owner}, principal)
    grant = live_mod.viewer_grant(
        st.settings, device_id, (device.get("streams") or "").split(","),
        datalake_url=getattr(st.settings, "datalake_url", "") or "")
    if grant is None:
        raise HTTPException(status_code=503, detail="live/viewing is not configured")
    return grant


@admin.get("/audit", responses={200: {"model": AuditList}})
def audit(request: Request, since: int = 0, limit: int = 100,
          principal: Principal = Depends(require_scope("observe"))):
    return {"events": request.app.state.metastore.read_audit(
        limit, since, account_id=principal.account_id)}

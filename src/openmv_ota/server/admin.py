"""The admin API -- rollouts + fleet observability. Token+scope-authed; every mutation audited.

(Release *publish* is in ``publish.py``; it needs the artifact codec.) Handlers read the metastore
off ``request.app.state`` and gate on a scope via ``require_scope``.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .auth import Principal, hash_token, require_scope
from .scopes import SCOPES

admin = APIRouter(prefix="/api/v1/admin")


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
    device_ids: list[str]


class DevicePin(BaseModel):
    release_id: str | None = None          # null unpins


class CohortPin(BaseModel):
    product_id: int
    cohort: str
    release_id: str | None = None          # null unpins (the account comes from the caller's token)


class AccountCreate(BaseModel):
    name: str


@admin.post("/accounts")
def create_account(body: AccountCreate, request: Request,
                   principal: Principal = Depends(require_scope("account:admin"))):
    """Operator-only (``account:admin``): create a tenant account + issue its first admin token.
    The remote equivalent of ``server account create``; the website (or a self-host super-admin)
    drives it. The token is returned once and only its hash is stored."""
    ms = request.app.state.metastore
    account_id = "acct_" + secrets.token_hex(8)
    token = secrets.token_urlsafe(32)
    ms.add_account(account_id, body.name)
    ms.add_token(hash_token(token), body.name, list(SCOPES), account_id=account_id)
    ms.append_audit(actor=principal.name, action="account.create", entity_type="account",
                    entity_id=account_id, data={"name": body.name},
                    account_id=principal.account_id)
    return {"account_id": account_id, "name": body.name, "token": token}


@admin.get("/accounts")
def list_accounts(request: Request,
                  principal: Principal = Depends(require_scope("account:admin"))):
    return {"accounts": request.app.state.metastore.list_accounts()}


@admin.post("/rollouts")
def create_rollout(body: RolloutCreate, request: Request,
                   principal: Principal = Depends(require_scope("rollout:control"))):
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


@admin.patch("/rollouts/{rollout_id}")
def patch_rollout(rollout_id: str, body: RolloutPatch, request: Request,
                  principal: Principal = Depends(require_scope("rollout:control"))):
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
        changes["state"] = body.state
    if not changes:
        raise HTTPException(status_code=400, detail="nothing to change")
    ms.update_rollout(rollout_id, **changes)
    ms.append_audit(actor=principal.name, action="rollout.update", entity_type="rollout",
                    entity_id=rollout_id, data=changes, account_id=principal.account_id)
    return ms.get_rollout(rollout_id)


@admin.post("/rollouts/{rollout_id}/rollback")
def rollback_rollout(rollout_id: str, request: Request,
                     principal: Principal = Depends(require_scope("rollout:control"))):
    ms = request.app.state.metastore
    _owned(ms.get_rollout(rollout_id), principal)
    ms.update_rollout(rollout_id, state="rolled_back")   # stops offering; does not downgrade
    ms.append_audit(actor=principal.name, action="rollout.rollback", entity_type="rollout",
                    entity_id=rollout_id, account_id=principal.account_id)
    return {"rollout_id": rollout_id, "state": "rolled_back"}


@admin.get("/rollouts")
def list_rollouts(request: Request, product_id: int | None = None, limit: int | None = None,
                  offset: int = 0, principal: Principal = Depends(require_scope("fleet:read"))):
    return {"rollouts": request.app.state.metastore.list_rollouts(
        product_id, account_id=principal.account_id, limit=limit, offset=offset)}


@admin.get("/rollouts/{rollout_id}/status")
def rollout_status(rollout_id: str, request: Request,
                   principal: Principal = Depends(require_scope("fleet:read"))):
    ro = _owned(request.app.state.metastore.get_rollout(rollout_id), principal)
    rate = (ro["updated"] / ro["attempted"]) if ro["attempted"] else None
    return {"rollout_id": rollout_id, "state": ro["state"], "percent": ro["percent"],
            "attempted": ro["attempted"], "updated": ro["updated"], "failures": ro["failures"],
            "success_rate": rate,
            # explicit device reports (POST /feedback) for this rollout's release
            "reported": request.app.state.metastore.deployment_counts(ro["release_id"])}


@admin.get("/cohorts")
def list_cohorts(request: Request, product_id: int | None = None,
                 principal: Principal = Depends(require_scope("fleet:read"))):
    return {"cohorts": request.app.state.metastore.list_cohorts(
        product_id, account_id=principal.account_id)}


@admin.post("/cohorts/assign")
def assign_cohort(body: CohortAssign, request: Request,
                  principal: Principal = Depends(require_scope("rollout:control"))):
    ms = request.app.state.metastore
    # scoped to the caller's account: an id belonging to another account is silently skipped
    n = ms.assign_cohort(body.device_ids, body.cohort, account_id=principal.account_id)
    ms.append_audit(actor=principal.name, action="cohort.assign", entity_type="cohort",
                    entity_id=body.cohort, data={"assigned": n, "requested": len(body.device_ids)},
                    account_id=principal.account_id)
    return {"cohort": body.cohort, "assigned": n}


def _check_pin_release(ms, release_id, principal):
    """If the pin targets an *existing* release, it must belong to the caller's account (else the
    device could be handed another account's signed bytes). A None/not-yet-published release_id is
    allowed -- the device path simply holds until such a release exists (and the device-path guard
    re-checks the account when it does)."""
    if release_id is not None:
        rel = ms.get_release(release_id)
        if rel is not None and rel.get("account_id", "") != principal.account_id:
            raise HTTPException(status_code=404)


@admin.patch("/devices/{device_id}/pin")
def pin_device(device_id: str, body: DevicePin, request: Request,
               principal: Principal = Depends(require_scope("rollout:control"))):
    ms = request.app.state.metastore
    _owned(ms.get_device(device_id), principal)              # 404 if missing or another account's
    _check_pin_release(ms, body.release_id, principal)
    ms.set_device_pin(device_id, body.release_id)            # release_id=None unpins
    ms.append_audit(actor=principal.name, action="device.pin", entity_type="device",
                    entity_id=device_id, data={"release_id": body.release_id},
                    account_id=principal.account_id)
    return {"device_id": device_id, "pinned_release_id": body.release_id}


@admin.post("/devices/{device_id}/account")
def bind_device(device_id: str, request: Request,
                principal: Principal = Depends(require_scope("rollout:control"))):
    """Operator override: (re)bind a device to the caller's account -- the authority for
    re-accounting a device or recovering one wrongly *learned* onto another account (which the
    signature already stops from installing anything). A device already *admin*-bound to a different
    account is 404 (not yours; no existence leak), so one account can't steal another's binding via
    the API. On a shared server, gate who may call this by proof of ownership (see threat-model)."""
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


@admin.post("/cohorts/pin")
def pin_cohort(body: CohortPin, request: Request,
               principal: Principal = Depends(require_scope("rollout:control"))):
    ms = request.app.state.metastore
    _check_pin_release(ms, body.release_id, principal)
    ms.set_cohort_pin(body.product_id, body.cohort, body.release_id,
                      account_id=principal.account_id)       # account from the token, not the body
    ms.append_audit(actor=principal.name, action="cohort.pin", entity_type="cohort",
                    entity_id=body.cohort, data={"product_id": body.product_id,
                                                 "release_id": body.release_id},
                    account_id=principal.account_id)
    return {"product_id": body.product_id, "cohort": body.cohort, "release_id": body.release_id}


@admin.get("/fleet")
def fleet(request: Request, product_id: int | None = None,
          principal: Principal = Depends(require_scope("fleet:read"))):
    return request.app.state.metastore.fleet_summary(product_id, account_id=principal.account_id)


@admin.get("/releases")
def releases(request: Request, product_id: int | None = None, limit: int | None = None,
             offset: int = 0, principal: Principal = Depends(require_scope("fleet:read"))):
    return {"releases": request.app.state.metastore.list_releases(
        product_id, account_id=principal.account_id, limit=limit, offset=offset)}


@admin.get("/devices")
def devices(request: Request, product_id: int | None = None, limit: int = 100,
            cohort: str | None = None, offset: int = 0,
            principal: Principal = Depends(require_scope("fleet:read"))):
    return {"devices": request.app.state.metastore.list_devices(
        product_id, limit, account_id=principal.account_id, cohort=cohort, offset=offset)}


@admin.get("/audit")
def audit(request: Request, since: int = 0, limit: int = 100,
          principal: Principal = Depends(require_scope("fleet:read"))):
    return {"events": request.app.state.metastore.read_audit(
        limit, since, account_id=principal.account_id)}

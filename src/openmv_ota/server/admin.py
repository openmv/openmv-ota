"""The admin API -- rollouts + fleet observability. Token+scope-authed; every mutation audited.

(Release *publish* is in ``publish.py``; it needs the artifact codec.) Handlers read the metastore
off ``request.app.state`` and gate on a scope via ``require_scope``.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .auth import Principal, require_scope

admin = APIRouter(prefix="/api/v1/admin")


def new_id(prefix: str) -> str:
    return "%s_%s" % (prefix, secrets.token_hex(8))


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
    board_id: int
    cohort: str
    release_id: str | None = None          # null unpins


@admin.post("/rollouts")
def create_rollout(body: RolloutCreate, request: Request,
                   principal: Principal = Depends(require_scope("rollout:control"))):
    ms = request.app.state.metastore
    rel = ms.get_release(body.release_id)
    if rel is None:
        raise HTTPException(status_code=404, detail="no such release")
    board_id = rel["board_id"]
    prior = ms.active_rollout(board_id, body.cohort)     # only one active per (board_id, cohort)
    if prior is not None:
        ms.update_rollout(prior["rollout_id"], state="paused")
        ms.append_audit(actor=principal.name, action="rollout.superseded", entity_type="rollout",
                        entity_id=prior["rollout_id"])
    rid = new_id("ro")
    ms.add_rollout(rollout_id=rid, release_id=body.release_id, board_id=board_id,
                   cohort=body.cohort, percent=body.percent,
                   failure_threshold=body.failure_threshold)
    ms.append_audit(actor=principal.name, action="rollout.create", entity_type="rollout",
                    entity_id=rid, data={"release_id": body.release_id, "cohort": body.cohort,
                                         "percent": body.percent})
    return {"rollout_id": rid, "board_id": board_id, "cohort": body.cohort,
            "percent": body.percent, "state": "active"}


@admin.patch("/rollouts/{rollout_id}")
def patch_rollout(rollout_id: str, body: RolloutPatch, request: Request,
                  principal: Principal = Depends(require_scope("rollout:control"))):
    ms = request.app.state.metastore
    ro = ms.get_rollout(rollout_id)
    if ro is None:
        raise HTTPException(status_code=404)
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
                    entity_id=rollout_id, data=changes)
    return ms.get_rollout(rollout_id)


@admin.post("/rollouts/{rollout_id}/rollback")
def rollback_rollout(rollout_id: str, request: Request,
                     principal: Principal = Depends(require_scope("rollout:control"))):
    ms = request.app.state.metastore
    if ms.get_rollout(rollout_id) is None:
        raise HTTPException(status_code=404)
    ms.update_rollout(rollout_id, state="rolled_back")   # stops offering; does not downgrade
    ms.append_audit(actor=principal.name, action="rollout.rollback", entity_type="rollout",
                    entity_id=rollout_id)
    return {"rollout_id": rollout_id, "state": "rolled_back"}


@admin.get("/rollouts")
def list_rollouts(request: Request, board_id: int | None = None,
                  principal: Principal = Depends(require_scope("fleet:read"))):
    return {"rollouts": request.app.state.metastore.list_rollouts(board_id)}


@admin.get("/rollouts/{rollout_id}/status")
def rollout_status(rollout_id: str, request: Request,
                   principal: Principal = Depends(require_scope("fleet:read"))):
    ro = request.app.state.metastore.get_rollout(rollout_id)
    if ro is None:
        raise HTTPException(status_code=404)
    rate = (ro["updated"] / ro["attempted"]) if ro["attempted"] else None
    return {"rollout_id": rollout_id, "state": ro["state"], "percent": ro["percent"],
            "attempted": ro["attempted"], "updated": ro["updated"], "failures": ro["failures"],
            "success_rate": rate,
            # explicit device reports (POST /feedback) for this rollout's release
            "reported": request.app.state.metastore.deployment_counts(ro["release_id"])}


@admin.get("/cohorts")
def list_cohorts(request: Request, board_id: int | None = None,
                 principal: Principal = Depends(require_scope("fleet:read"))):
    return {"cohorts": request.app.state.metastore.list_cohorts(board_id)}


@admin.post("/cohorts/assign")
def assign_cohort(body: CohortAssign, request: Request,
                  principal: Principal = Depends(require_scope("rollout:control"))):
    ms = request.app.state.metastore
    n = ms.assign_cohort(body.device_ids, body.cohort)       # UPDATE existing rows only; missing ids skipped
    ms.append_audit(actor=principal.name, action="cohort.assign", entity_type="cohort",
                    entity_id=body.cohort, data={"assigned": n, "requested": len(body.device_ids)})
    return {"cohort": body.cohort, "assigned": n}


@admin.patch("/devices/{device_id}/pin")
def pin_device(device_id: str, body: DevicePin, request: Request,
               principal: Principal = Depends(require_scope("rollout:control"))):
    ms = request.app.state.metastore
    if ms.get_device(device_id) is None:
        raise HTTPException(status_code=404)
    ms.set_device_pin(device_id, body.release_id)            # release_id=None unpins
    ms.append_audit(actor=principal.name, action="device.pin", entity_type="device",
                    entity_id=device_id, data={"release_id": body.release_id})
    return {"device_id": device_id, "pinned_release_id": body.release_id}


@admin.post("/cohorts/pin")
def pin_cohort(body: CohortPin, request: Request,
               principal: Principal = Depends(require_scope("rollout:control"))):
    ms = request.app.state.metastore
    ms.set_cohort_pin(body.board_id, body.cohort, body.release_id)   # release_id=None unpins
    ms.append_audit(actor=principal.name, action="cohort.pin", entity_type="cohort",
                    entity_id=body.cohort, data={"board_id": body.board_id, "release_id": body.release_id})
    return {"board_id": body.board_id, "cohort": body.cohort, "release_id": body.release_id}


@admin.get("/fleet")
def fleet(request: Request, board_id: int | None = None,
          principal: Principal = Depends(require_scope("fleet:read"))):
    return request.app.state.metastore.fleet_summary(board_id)


@admin.get("/devices")
def devices(request: Request, board_id: int | None = None, limit: int = 100,
            principal: Principal = Depends(require_scope("fleet:read"))):
    return {"devices": request.app.state.metastore.list_devices(board_id, limit)}


@admin.get("/audit")
def audit(request: Request, since: int = 0, limit: int = 100,
          principal: Principal = Depends(require_scope("fleet:read"))):
    return {"events": request.app.state.metastore.read_audit(limit, since)}

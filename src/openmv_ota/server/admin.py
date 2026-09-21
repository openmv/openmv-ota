"""The admin API -- rollouts + fleet observability. Token+scope-authed; every mutation audited.

(Release *publish* is in ``publish.py``; it needs the artifact codec.) Handlers read the metastore
off ``request.app.state`` and gate on a scope via ``require_scope``.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response, Query
from pydantic import BaseModel

from . import datalog as datalog_mod
from . import live as live_mod
from .auth import Principal, hash_token, require_scope
from .schemas import (
    Account,
    AccountActive,
    AccountCreated,
    AccountLimited,
    AccountList,
    AccountNamed,
    AdvisoryList,
    Product, ProductDeclared, ProductList, ProductRenamed, ProductViewerGrant, ViewerGrants,
    AdvisoryScan,
    AuditList,
    Cohort,
    CohortAssigned,
    CohortList,
    CohortPinned,
    CohortDeleted,
    CohortCreated,
    CohortRenamed,
    Device,
    DeviceBound,
    DeviceForgotten,
    DeviceList,
    DevicePinned,
    ActivityList,
    FleetBases,
    FleetSummary,
    InstallDays,
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
from .scopes import ACCOUNT_ROOT, ALL_SCOPES, SCOPES, expand

admin = APIRouter(prefix="/api/v1/admin")


# Default page size for the paginated admin lists. `/devices` already capped at 100; `/releases`
# and `/rollouts` defaulted to NO limit, so a fleet with thousands of releases returned all of
# them in one response. Same number everywhere is the point -- a caller should not have to
# remember which collection happens to be unbounded.
_PAGE = 100
# The most rows one request may ask for. Without a ceiling, `?limit=100000000` makes
# the server build a hundred million dicts -- on a SHARED server, so one account's
# token can starve every other fleet's update service. Paging past it with `offset`
# still reads everything; asking for all of it in one breath does not.
_MAX_PAGE = 1000
_LIMIT_Q = Query(_PAGE, ge=1, le=_MAX_PAGE, description="rows per page (max %d)" % _MAX_PAGE)

# The list contract every collection endpoint follows (documented on the API page via
# these Query descriptions): ?sort=<column>&dir=asc|desc&limit&offset plus the list's
# own filters, returning the page and a `total` that respects those filters. Unknown
# sort keys fall back to the list's natural order -- never an error, never raw SQL.
def _sort_q(cols: str):
    return Query(None, description="sort column: one of " + cols)


_DIR_Q = Query("asc", description="sort direction: asc or desc (with sort)")


def new_id(prefix: str) -> str:
    return "%s_%s" % (prefix, secrets.token_hex(8))


def _may_product(product_id, principal):
    """404 unless this credential may act on ``product_id``.

    For paths where the caller NAMES a product rather than fetching an entity that
    carries one: assigning a cohort by product, pinning one, renaming a product, minting
    a product grant, publishing. 404 rather than 403, so a limited token cannot use the
    error to learn which products exist."""
    if not principal.may(product_id):
        raise HTTPException(status_code=404)
    return product_id


def _owned(entity, principal):
    """Return ``entity`` iff this credential may act on it; else 404 -- a missing entity and
    one the caller may not see are indistinguishable, so probing leaks nothing.

    Two gates, not one. The account gate keeps tenants apart. The product gate keeps a
    product-limited token inside its products: a platform that models each of its own
    customers as a product can hand out a credential per customer, and this is what makes
    that boundary real for entities fetched by id, where no list filter applies."""
    if entity is None or entity.get("account_id", "") != principal.account_id:
        raise HTTPException(status_code=404)
    if "product_id" in entity and not principal.may(entity["product_id"]):
        raise HTTPException(status_code=404)
    return entity


class RolloutCreate(BaseModel):
    release_id: str
    cohort: str = "__default__"
    percent: float
    failure_threshold: float = 0.05
    display_name: str = ""                 # a label only; need not be unique


class RolloutPatch(BaseModel):
    percent: float | None = None
    state: str | None = None
    failure_threshold: float | None = None   # 0..1; changing it never resumes a paused rollout


class CohortAssign(BaseModel):
    cohort: str
    device_ids: list[str] | None = None    # surgical: these exact devices
    product_id: int | None = None          # bulk: every device of this product


class CohortCreate(BaseModel):
    cohort: str                            # the label to declare (no devices yet)


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
    client_ref: str | None = None
    """The caller's own id for this account (a workspace id in the platform that is
    provisioning it). Optional, and the thing that makes creation safe to retry: ask
    again with the same `client_ref` and the account you already made comes back, with
    `created: false` and no new token, instead of a second account or a 409 you cannot
    tell apart from someone else's name."""


def _owner(principal) -> str | None:
    """The operator identity accounts are filed under, or None for the server's own root.

    A token with ``accounts.all`` is the operator of the SERVER and sees everything. Any
    other ``accounts`` token is an operator of its own customers -- our website, or a
    platform reselling this server -- and is filed under its token name."""
    return None if ACCOUNT_ROOT in principal.scopes else principal.name


def _clean_name(ms, name, except_id=None, owner=None):
    """A non-empty account name, unique among ``owner``'s accounts (case-insensitive), or an
    HTTPException (400 empty / 409 taken). Shared by create + rename so both enforce the same
    rule.

    Uniqueness stops at the operator who created the account. Server-wide uniqueness read as
    one namespace for everyone, which is wrong twice over: two platforms reselling this
    server cannot both have a customer called "Acme", and the 409 telling them so is a way
    to ask whether the other platform has one."""
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="account name must not be empty")
    if ms.account_name_exists(name, except_id, created_by=owner):
        raise HTTPException(status_code=409, detail="an account named %r already exists" % name)
    return name


def _owned_account(ms, account_id: str, principal):
    """The account, or 404 unless this credential provisioned it.

    404 and not 403: an operator must not be able to learn that another operator's
    account exists by the shape of the refusal."""
    acc = ms.get_account(account_id)
    owner = _owner(principal)
    if acc is None or (owner is not None and (acc.get("created_by") or "") != owner):
        raise HTTPException(status_code=404)
    return acc


@admin.post("/accounts", responses={200: {"model": AccountCreated}})
def create_account(body: AccountCreate, request: Request,
                   principal: Principal = Depends(require_scope("accounts"))):
    """Operator-only (``accounts``): create a tenant account + issue its first admin token.
    The remote equivalent of ``server account create``; the website, a self-host
    super-admin, or a platform provisioning its own customers drives it. The token is
    returned once and only its hash is stored.

    The account is filed under the credential that created it. That operator then sees it
    in `GET /accounts` and may manage it; another operator on the same server cannot, and
    cannot see that it exists.

    Pass `client_ref` and the call becomes idempotent -- see the field."""
    ms = request.app.state.metastore
    owner = _owner(principal) or principal.name
    if body.client_ref:
        seen = ms.account_by_client_ref(owner, body.client_ref)
        if seen is not None:
            # The token is NOT reissued: it was handed over once, the caller either kept
            # it or rotates it, and minting a fresh one on a retry would leave a live
            # credential nobody is tracking.
            return {"account_id": seen["account_id"], "name": seen["name"], "token": None,
                    "created": False, "client_ref": body.client_ref}
    name = _clean_name(ms, body.name, owner=_owner(principal))
    account_id = "acct_" + secrets.token_hex(8)
    token = secrets.token_urlsafe(32)
    ms.add_account(account_id, name, created_by=owner, client_ref=body.client_ref or "")
    ms.add_token(hash_token(token), name, list(SCOPES), account_id=account_id)
    ms.append_audit(actor=principal.name, action="account.create", entity_type="account",
                    entity_id=account_id, data={"name": name},
                    account_id=principal.account_id)
    return {"account_id": account_id, "name": name, "token": token, "created": True,
            "client_ref": body.client_ref or ""}


@admin.get("/accounts", responses={200: {"model": AccountList}})
def list_accounts(request: Request,
                  q: str | None = Query(None, description="name, id or client_ref contains"),
                  active: bool | None = Query(None, description="only active (true) or "
                                              "deactivated (false) accounts"),
                  limit: int = Query(_MAX_PAGE, ge=1, le=_MAX_PAGE), offset: int = 0,
                  principal: Principal = Depends(require_scope("accounts"))):
    """The operator's account directory. Operator scope, not an account credential:
    a normal account token can neither list accounts nor see another one exists.

    An `accounts` credential sees the accounts IT provisioned. Only `accounts.all` -- the
    server's own root -- sees every account on the server. Without that split, handing a
    partner the ability to create customers also hands them the customer list of everyone
    else on the server.

    Each row carries what a directory shows beside a name: registered devices, releases,
    active rollouts and the newest check-in. `q` searches, `active` narrows to live or
    deactivated accounts, `limit`/`offset` page, and `total` counts what the filters
    matched -- so "how many accounts are switched off" is `active=false&limit=1`."""
    ms = request.app.state.metastore
    owner = _owner(principal)
    rows = ms.list_accounts(created_by=owner, q=q, limit=limit, offset=offset, active=active)
    counts = ms.account_counts(r["account_id"] for r in rows)
    for r in rows:
        r.update(counts.get(r["account_id"], {}))
    return {"accounts": rows,
            "total": ms.count_accounts(created_by=owner, q=q, active=active)}


@admin.get("/accounts/{account_id}", responses={200: {"model": Account}})
def get_account(account_id: str, request: Request,
                principal: Principal = Depends(require_scope("accounts"))):
    """One account's directory row -- the same shape the listing carries, without the
    listing: a page about one tenant reads its own row, never all of them. 404 unless
    this credential provisioned it (or holds the root scope)."""
    ms = request.app.state.metastore
    row = dict(_owned_account(ms, account_id, principal))
    row.update(ms.account_counts([account_id]).get(account_id, {}))
    return row


@admin.get("/devices/lookup", responses={200: {"model": DeviceList}})
def lookup_devices(request: Request, q: str = Query(..., min_length=2, max_length=128),
                   limit: int = Query(50, ge=1, le=_MAX_PAGE), offset: int = 0,
                   principal: Principal = Depends(require_scope(ACCOUNT_ROOT))):
    """Find a camera across EVERY account, by a fragment of its id or its display name.
    The server's root only: "which account is this device in" is the first question a
    support request asks, and no account credential can answer it about a device that is
    not its own (that answers 404, as it should)."""
    ms = request.app.state.metastore
    return {"devices": _with_fallback_version(ms.list_devices(q=q, limit=limit, offset=offset)),
            "total": ms.count_devices(q=q)}


class AccountPatch(BaseModel):
    name: str


@admin.patch("/accounts/{account_id}", responses={200: {"model": AccountNamed}})
def patch_account(account_id: str, body: AccountPatch, request: Request,
                  principal: Principal = Depends(require_scope("accounts"))):
    """Rename an account or change its contact address. Operator scope."""
    ms = request.app.state.metastore
    _owned_account(ms, account_id, principal)
    name = _clean_name(ms, body.name, except_id=account_id, owner=_owner(principal))
    ms.rename_account(account_id, name)
    ms.append_audit(actor=principal.name, action="account.rename", entity_type="account",
                    entity_id=account_id, data={"name": name}, account_id=principal.account_id)
    return {"account_id": account_id, "name": name}


class AccountLimit(BaseModel):
    device_limit: int | None = None        # null = unlimited


@admin.put("/accounts/{account_id}/limit", responses={200: {"model": AccountLimited}})
def set_account_limit(account_id: str, body: AccountLimit, request: Request,
                      principal: Principal = Depends(require_scope("accounts"))):
    """The account's device entitlement (operator-only): how many devices may register.
    Enforced for NEW devices at check-in; devices already registered are never dropped.
    ``null`` lifts the limit."""
    ms = request.app.state.metastore
    _owned_account(ms, account_id, principal)
    if body.device_limit is not None and body.device_limit < 0:
        raise HTTPException(status_code=400, detail="device_limit must be >= 0 or null")
    ms.set_device_limit(account_id, body.device_limit)
    ms.append_audit(actor=principal.name, action="account.limit", entity_type="account",
                    entity_id=account_id, data={"device_limit": body.device_limit},
                    account_id=principal.account_id)
    return {"account_id": account_id, "device_limit": body.device_limit,
            "devices": ms.device_count(account_id)}


@admin.post("/accounts/{account_id}/deactivate", responses={200: {"model": AccountActive}})
def deactivate_account(account_id: str, request: Request, body: TokenActor | None = None,
                       principal: Principal = Depends(require_scope("accounts"))):
    """Soft off-switch: revoke every token + set active=0. Admin access dies; fielded devices keep
    being served (a billing lapse doesn't brick a fleet), and no new token can be minted until the
    account is reactivated.

    `actor` names the person on whose behalf an operator credential is calling, as
    token revoke and rotate already do; the operator is kept as `via`."""
    ms = request.app.state.metastore
    _owned_account(ms, account_id, principal)
    n = ms.revoke_account_tokens(account_id)
    ms.set_account_active(account_id, False)
    # `actor` names the PERSON, the way revoke and rotate already allow: a console
    # calls this with an operator credential, and "the operator retired the account"
    # loses the one fact worth keeping about the end of a tenant -- who ended it. This
    # log is also the only one that survives it, since the tenant's own rows go too.
    who, via = _audit_actor(principal, body.actor if body else None)
    ms.append_audit(actor=who, action="account.deactivate", entity_type="account",
                    entity_id=account_id, data={"tokens_revoked": n, **via},
                    account_id=principal.account_id)
    return {"account_id": account_id, "active": False, "tokens_revoked": n}


@admin.post("/accounts/{account_id}/activate", responses={200: {"model": AccountActive}})
def activate_account(account_id: str, request: Request,
                     principal: Principal = Depends(require_scope("accounts"))):
    """Re-enable an account (active=1). Does NOT un-revoke old tokens -- issue fresh ones."""
    ms = request.app.state.metastore
    _owned_account(ms, account_id, principal)
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
    products: list[int] | None = None
    """Limit the token to these product ids. Omitted (or empty) is the whole account,
    which is what an ordinary token wants. A platform that models each of its own
    customers as a product issues one limited token per customer: `scopes` says what it
    may do, `products` says what it may do it to. Every read is filtered to the list and
    anything outside it answers 404, exactly as another account's would."""
    actor: str | None = None
    """Who is really doing this, for the audit log, when an operator credential acts on a
    person's behalf (a web console). The operator's own name is kept as ``via``."""


class TokenActor(BaseModel):
    actor: str | None = None               # same as TokenIssue.actor, for revoke/rotate


def _audit_actor(principal, hint):
    """(actor, extra): the person named by ``hint`` acting via the operator, else the operator."""
    if hint:
        return hint, {"via": principal.name}
    return principal.name, {}


def _mint(ms, principal, name, scopes, account_id, action, extra=None, actor=None,
          products=()):
    token = secrets.token_urlsafe(32)
    th = hash_token(token)
    ms.add_token(th, name, scopes, account_id=account_id, products=products)
    who, via = _audit_actor(principal, actor)
    # Recorded under the TOKEN's account, not the caller's: tokens are minted by an operator
    # credential (account "" ), and the tenant is who needs to see it in their audit log.
    ms.append_audit(actor=who, action=action, entity_type="token", entity_id=th,
                    data={"account_id": account_id, "name": name, **via, **(extra or {})},
                    account_id=account_id)
    return {"token_hash": th, "name": name, "scopes": scopes, "account_id": account_id,
            "products": list(products), "token": token}


def _active_account(ms, account_id, principal):
    """The account, requiring that this credential provisioned it (404) and that it is active
    (409). Gate for minting tokens -- a deactivated account must never get a fresh working
    credential (issue *or* rotate), and one operator must never mint a credential into
    another operator's account."""
    acc = _owned_account(ms, account_id, principal)
    if not acc["active"]:
        raise HTTPException(status_code=409, detail="account is deactivated")
    return acc


@admin.post("/accounts/{account_id}/tokens", responses={200: {"model": TokenIssued}})
def issue_token(account_id: str, body: TokenIssue, request: Request,
                principal: Principal = Depends(require_scope("accounts"))):
    """Mint an admin token for an account. The token is returned **once, in full** --
    only its hash is stored, so a lost token is rotated, never recovered.

    `scopes` defaults to every scope; pass the narrowest that works. The ladder is
    `publish` > `manage` > `observe`, so a CI job that only publishes needs `publish`,
    and a dashboard that only reads needs `observe`. `accounts` is the operator scope
    and is not implied by any of the others."""
    ms = request.app.state.metastore
    _active_account(ms, account_id, principal)                 # 404 not ours / 409 deactivated
    scopes = body.scopes if body.scopes is not None else list(SCOPES)
    bad = [s for s in scopes if s not in ALL_SCOPES]
    if bad:
        raise HTTPException(status_code=400, detail="unknown scope(s): %s" % ", ".join(bad))
    if ms.token_name_in_use(account_id, body.name):
        raise HTTPException(status_code=409, detail="token name already in use: %s" % body.name)
    products = body.products or []
    if products and "accounts" in expand(scopes):
        # The operator scope acts across accounts, where a product list means nothing --
        # allowing both would read as a limit that is not one.
        raise HTTPException(status_code=400,
                            detail="a product-scoped token cannot carry the accounts scope")
    return _mint(ms, principal, body.name, expand(scopes), account_id, "token.issue",
                 actor=body.actor, products=products,
                 extra={"products": products} if products else None)


@admin.get("/accounts/{account_id}/tokens", responses={200: {"model": TokenList}})
def list_account_tokens(account_id: str, request: Request,
                        principal: Principal = Depends(require_scope("accounts"))):
    """The account's tokens: name, scopes, when issued, whether revoked -- never the
    token itself, which exists only in the response that created it."""
    ms = request.app.state.metastore
    _owned_account(ms, account_id, principal)
    return {"tokens": ms.list_tokens(account_id=account_id)}   # metadata only -- never the secret


@admin.post("/tokens/{token_hash}/revoke", responses={200: {"model": TokenRevoked}})
def revoke_token(token_hash: str, request: Request, body: TokenActor | None = None,
                 principal: Principal = Depends(require_scope("accounts"))):
    """Revoke a token by its hash. Immediate: the next request carrying it is 401.
    Revocation is recorded in the audit log with the actor who did it."""
    ms = request.app.state.metastore
    old = ms.get_token(token_hash)
    if old is None:
        raise HTTPException(status_code=404)
    _owned_account(ms, old["account_id"], principal)    # not ours: not even its existence
    ms.revoke_token(token_hash)
    who, via = _audit_actor(principal, body.actor if body else None)
    ms.append_audit(actor=who, action="token.revoke", entity_type="token",
                    entity_id=token_hash, data={"name": old["name"], **via},
                    account_id=old["account_id"])                # the token's account, see _mint
    return {"token_hash": token_hash, "revoked": True}


@admin.post("/tokens/{token_hash}/rotate", responses={200: {"model": TokenIssued}})
def rotate_token(token_hash: str, request: Request, body: TokenActor | None = None,
                 principal: Principal = Depends(require_scope("accounts"))):
    """Issue a replacement (same name/scopes/account) and revoke the old one -- the recovery path
    for a lost/leaked token. Returns the new secret once."""
    ms = request.app.state.metastore
    old = ms.get_token(token_hash)
    if old is None:
        raise HTTPException(status_code=404)
    _active_account(ms, old["account_id"], principal)          # nor into one that is not ours
    fresh = _mint(ms, principal, old["name"], expand(old["scopes"]), old["account_id"], "token.rotate",
                  extra={"replaced": token_hash}, actor=body.actor if body else None)
    ms.revoke_token(token_hash)
    return fresh


class PublishSeq(BaseModel):
    publish_seq: int


@admin.post("/publish-seq", responses={200: {"model": PublishSeq}})
def allocate_publish_seq(request: Request,
                         principal: Principal = Depends(require_scope("publish"))):
    """Allocate this account's next publish counter -- the number a build stamps into the
    image it is about to sign.

    Only a fleet whose cameras are built with ``product_id = 0`` needs one. Those cameras
    can be moved between product lines, so their images cannot be ordered by a per-product
    version; they are ordered by this, which spans the account. Every other project leaves
    the field at 0 and never calls this.

    It is allocated here because a counter is the one part of a build that cannot be copied
    the way a signing key can: two builds must never take the same number. Gaps are
    expected and harmless -- a build that fails after taking one simply burns it.
    """
    seq = request.app.state.metastore.next_publish_seq(principal.account_id)
    if seq is None:
        raise HTTPException(status_code=409,
                            detail="no publish counter for this credential's account; "
                                   "platform builds need a real account, not a self-host's "
                                   "implicit one")
    return {"publish_seq": seq}


@admin.post("/rollouts", responses={200: {"model": RolloutCreated}})
def create_rollout(body: RolloutCreate, request: Request,
                   principal: Principal = Depends(require_scope("manage"))):
    """Offer an already-published release to a cohort, a percentage at a time.

    One active rollout per product and cohort: creating a second supersedes the first,
    which is paused with `pause_reason: "superseded"` rather than deleted, so the
    history stays readable. `percent` is the share of that cohort offered the release
    now -- raise it with `PATCH /rollouts/{rollout_id}` as the numbers come in.

    `failure_threshold` is the fraction of offered devices that may fall back before
    the rollout pauses itself (`pause_reason: "failure_limit"`). It is the safety net
    that makes a staged rollout meaningfully different from flipping every device at
    once; the default is 0.05."""
    ms = request.app.state.metastore
    rel = _owned(ms.get_release(body.release_id), principal)   # 404 if missing or another account's
    product_id = rel["product_id"]
    account_id = principal.account_id                      # the rollout inherits the caller's account
    prior = ms.active_rollout(product_id, body.cohort, account_id=account_id)   # one active per (account, product, cohort)
    if prior is not None:
        ms.update_rollout(prior["rollout_id"], state="paused", pause_reason="superseded")
        ms.append_audit(actor=principal.name, action="rollout.superseded", entity_type="rollout",
                        entity_id=prior["rollout_id"], account_id=account_id,
                        product_id=product_id)
    display_name = _label(body.display_name)
    rid = new_id("ro")
    ms.add_rollout(rollout_id=rid, release_id=body.release_id, product_id=product_id,
                   cohort=body.cohort, percent=body.percent,
                   failure_threshold=body.failure_threshold, account_id=account_id,
                   display_name=display_name)
    ms.append_audit(actor=principal.name, action="rollout.create", entity_type="rollout",
                    entity_id=rid, data={"release_id": body.release_id, "cohort": body.cohort,
                                         "percent": body.percent}, account_id=account_id,
                    product_id=product_id)
    return {"rollout_id": rid, "product_id": product_id, "product_id_str": str(product_id),
            "cohort": body.cohort,
            "percent": body.percent, "state": "active", "display_name": display_name}


@admin.patch("/rollouts/{rollout_id}", responses={200: {"model": Rollout}})
def patch_rollout(rollout_id: str, body: RolloutPatch, request: Request,
                  principal: Principal = Depends(require_scope("manage"))):
    """Change a live rollout: raise `percent`, adjust `failure_threshold`, or move
    `state` between `active` and `paused`. Pausing by hand records
    `pause_reason: "operator"`, which is how the dashboard tells your decision apart
    from the auto-pause."""
    ms = request.app.state.metastore
    ro = _owned(ms.get_rollout(rollout_id), principal)
    changes: dict = {}
    if body.percent is not None:
        if body.percent < ro["percent"]:
            raise HTTPException(status_code=400, detail="percent is monotonic (can only rise)")
        changes["percent"] = body.percent
    if body.failure_threshold is not None:
        # the auto-pause limit as a fraction of offered devices; an operator raises it
        # after diagnosing spurious fallbacks, or tightens it for a risky build. It is
        # only the limit: a rollout paused by the old limit stays paused until resumed.
        if not 0 <= body.failure_threshold <= 1:
            raise HTTPException(status_code=400, detail="failure_threshold must be 0..1")
        changes["failure_threshold"] = body.failure_threshold
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
    ms.update_rollout(rollout_id, **changes,
                      **({"pause_reason": "operator" if body.state == "paused" else None}
                         if body.state is not None else {}))
    ms.append_audit(actor=principal.name, action="rollout.update", entity_type="rollout",
                    entity_id=rollout_id, data=changes, account_id=principal.account_id,
                    product_id=ro["product_id"])
    return ms.get_rollout(rollout_id)


@admin.post("/rollouts/{rollout_id}/stop", responses={200: {"model": RolloutState}})
def stop_rollout(rollout_id: str, request: Request,
                     principal: Principal = Depends(require_scope("manage"))):
    """Stop offering a release for good. Devices that already took it keep it -- this
    is not a downgrade, and there is no way to pull an installed release back. To move
    a fleet off a bad build, publish one that supersedes it."""
    ms = request.app.state.metastore
    ro = _owned(ms.get_rollout(rollout_id), principal)
    ms.update_rollout(rollout_id, state="stopped", pause_reason=None)   # stops offering; does not downgrade
    ms.append_audit(actor=principal.name, action="rollout.stop", entity_type="rollout",
                    entity_id=rollout_id, account_id=principal.account_id,
                    product_id=ro["product_id"])
    return {"rollout_id": rollout_id, "state": "stopped"}


# The list is pure ENUMERATION -- enough to find and recognize a rollout -- while
# /status is the complete single-rollout read (identity, policy, timestamps, counters,
# derived score). Everything specific to one rollout lives there, once.
_ROLLOUT_ROW = ("rollout_id", "release_id", "product_id", "product_id_str", "cohort",
                "percent", "state",
                "cohort_devices", "up_to_date", "pause_reason", "display_name")


@admin.get("/rollouts", responses={200: {"model": RolloutList}})
def list_rollouts(request: Request, product_id: int | None = None, limit: int = _LIMIT_Q,
                  offset: int = 0, state: str | None = None, cohort: str | None = None,
                  release_id: str | None = Query(None, description="only rollouts of this release"),
                  pause_reason: str | None = Query(
                      None, description="only rollouts paused for this reason: operator, "
                                        "superseded, failure_limit"),
                  sort: str | None = _sort_q("created, percent, state, cohort, product, name, "
                                             "devices, rollout"),
                  dir: str = _DIR_Q,
                  principal: Principal = Depends(require_scope("observe"))):
    """``cohort`` narrows to the rollouts targeting one label (any product) -- the
    question a cohort's page asks; combine with ``state`` for "live" vs "history".
    ``total`` counts what the filters match, not the whole table."""
    ms = request.app.state.metastore
    rows = ms.list_rollouts(product_id, account_id=principal.account_id,
                            limit=limit, offset=offset, state=state, cohort=cohort,
                            sort=sort, direction=dir, release_id=release_id,
                            pause_reason=pause_reason, products=principal.scoped())
    return {"rollouts": [{k: r[k] for k in _ROLLOUT_ROW} for r in rows],
            "total": ms.count_rollouts(product_id, principal.account_id, state, cohort,
                                       release_id, pause_reason, products=principal.scoped())}


@admin.get("/rollouts/{rollout_id}/status", responses={200: {"model": RolloutStatus}})
def rollout_status(rollout_id: str, request: Request,
                   principal: Principal = Depends(require_scope("observe"))):
    """One rollout's numbers: how many devices the cohort holds, how many were
    offered the release, how many confirmed it, and how many fell back. This is what to
    poll while a rollout is live, and what the failure threshold is measured against."""
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
                 limit: int | None = Query(None, ge=1, le=_MAX_PAGE),
                 offset: int = 0,
                 sort: str | None = _sort_q("cohort, devices, products, pins"),
                 dir: str = _DIR_Q,
                 principal: Principal = Depends(require_scope("observe"))):
    """The account's cohorts with a device count each. A cohort is a label on a
    device (`__default__` until you assign one), and it is the unit a rollout targets."""
    rows, total = request.app.state.metastore.page_cohorts(
        product_id, account_id=principal.account_id, sort=sort, direction=dir,
        limit=limit, offset=offset, products=principal.scoped())
    return {"cohorts": rows, "total": total}


@admin.get("/cohorts/{cohort}", responses={200: {"model": Cohort}})
def get_cohort(cohort: str, request: Request,
               principal: Principal = Depends(require_scope("observe"))):
    """One cohort's row: its device count, the split per product, its pins. 404 when no
    device is in it and nothing declared it."""
    rows, _ = request.app.state.metastore.page_cohorts(
        None, account_id=principal.account_id, products=principal.scoped())
    row = next((r for r in rows if r["cohort"] == cohort), None)
    if row is None:
        raise HTTPException(status_code=404)
    return row


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
    if body.product_id is not None:
        _may_product(body.product_id, principal)
    if body.device_ids is not None:
        n = ms.assign_cohort(body.device_ids, body.cohort, account_id=principal.account_id)
        # the WHICH, not just the how-many: a history view lists the devices moved
        # (bounded, so a giant bulk assign can't bloat one audit row)
        data = {"assigned": n, "requested": len(body.device_ids),
                "device_ids": body.device_ids[:100]}
        if len(body.device_ids) > 100:
            data["truncated"] = len(body.device_ids) - 100
    else:
        n = ms.assign_cohort_product(body.product_id, body.cohort,
                                     account_id=principal.account_id)
        data = {"assigned": n, "product_id": body.product_id}
    ms.append_audit(actor=principal.name, action="cohort.assign", entity_type="cohort",
                    entity_id=body.cohort, data=data, account_id=principal.account_id,
                    product_id=body.product_id)
    return {"cohort": body.cohort, "assigned": n}


@admin.post("/cohorts/create", responses={200: {"model": CohortCreated}})
def create_cohort(body: CohortCreate, request: Request,
                  principal: Principal = Depends(require_scope("manage"))):
    """Declare a cohort ahead of its first device, so an empty label exists to assign
    into (a label also springs into being implicitly on `assign`). ``__default__`` is
    refused (it always exists), and so is a name already in use anywhere -- on devices,
    rollouts, pins, or declared -- because two things called `beta` is a merge, not a
    creation."""
    ms = request.app.state.metastore
    cohort = (body.cohort or "").strip()
    if not cohort:
        raise HTTPException(status_code=400, detail="cohort name is required")
    if cohort == "__default__":
        raise HTTPException(status_code=400, detail="__default__ always exists")
    if ms.cohort_in_use(cohort, account_id=principal.account_id):
        raise HTTPException(status_code=409, detail="cohort %r is already in use" % cohort)
    ms.create_cohort(cohort, account_id=principal.account_id)
    ms.append_audit(actor=principal.name, action="cohort.create", entity_type="cohort",
                    entity_id=cohort, data={}, account_id=principal.account_id)
    return {"cohort": cohort}


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


def _label(name: str) -> str:
    """Validate a display label: stripped, max 64 chars, '' allowed (= cleared)."""
    name = name.strip()
    if len(name) > 64:
        raise HTTPException(status_code=400, detail="name too long (max 64)")
    return name


class DeviceName(BaseModel):
    name: str                              # display label; '' clears it


class DeviceRenamed(BaseModel):
    device_id: str
    display_name: str


@admin.patch("/devices/{device_id}/name", responses={200: {"model": DeviceRenamed}})
def rename_device(device_id: str, body: DeviceName, request: Request,
                  principal: Principal = Depends(require_scope("manage"))):
    """Set the device's operator-facing display name -- a pure label (the
    device_id stays the identity everywhere). '' clears it."""
    name = _label(body.name)
    ms = request.app.state.metastore
    dev = _owned(ms.get_device(device_id), principal)        # 404 if missing or another account's
    ms.set_device_name(device_id, name)
    ms.append_audit(actor=principal.name, action="device.rename", entity_type="device",
                    entity_id=device_id, data={"name": name},
                    account_id=principal.account_id,
                    product_id=(dev or {}).get("product_id"))
    return {"device_id": device_id, "display_name": name}


class ReleaseRenamed(BaseModel):
    release_id: str
    display_name: str


@admin.patch("/releases/{release_id}/name", responses={200: {"model": ReleaseRenamed}})
def rename_release(release_id: str, body: DeviceName, request: Request,
                   principal: Principal = Depends(require_scope("manage"))):
    """Set a release's display name -- a label for dashboards and lists, never
    identity (the release_id stays the key everywhere). '' clears it."""
    name = _label(body.name)
    ms = request.app.state.metastore
    rel = _owned(ms.get_release(release_id), principal)
    ms.set_release_name(release_id, name)
    ms.append_audit(actor=principal.name, action="release.rename", entity_type="release",
                    entity_id=release_id, data={"name": name},
                    account_id=principal.account_id, product_id=rel["product_id"])
    return {"release_id": release_id, "display_name": name}


class RolloutRenamed(BaseModel):
    rollout_id: str
    display_name: str


@admin.patch("/rollouts/{rollout_id}/name", responses={200: {"model": RolloutRenamed}})
def rename_rollout(rollout_id: str, body: DeviceName, request: Request,
                   principal: Principal = Depends(require_scope("manage"))):
    """Set a rollout's display name -- same label rules as releases."""
    name = _label(body.name)
    ms = request.app.state.metastore
    ro = _owned(ms.get_rollout(rollout_id), principal)
    ms.set_rollout_name(rollout_id, name)
    ms.append_audit(actor=principal.name, action="rollout.rename", entity_type="rollout",
                    entity_id=rollout_id, data={"name": name},
                    account_id=principal.account_id, product_id=ro["product_id"])
    return {"rollout_id": rollout_id, "display_name": name}


@admin.patch("/devices/{device_id}/pin", responses={200: {"model": DevicePinned}})
def pin_device(device_id: str, body: DevicePin, request: Request,
               principal: Principal = Depends(require_scope("manage"))):
    """Pin one device to a release, overriding any rollout, or clear the pin with
    `{"release_id": null}`. The pin wins over cohort pins and rollouts both, so this is
    how you hold a single unit on a known build -- a device on a bench, or one a
    customer is mid-incident with.

    **The device need not have checked in yet.** A pin is an intent about a device id, so
    it can be recorded when hardware ships and is waiting on that camera's very first
    check-in -- which is what a platform claiming a unit at the point of sale needs. An id
    already bound to another account is still a 404.
    """
    ms = request.app.state.metastore
    dev = ms.get_device(device_id)
    if dev is not None:
        _owned(dev, principal)                              # 404 if another account's
    else:
        # Never seen. The only thing to check is that the id is not already spoken for:
        # without a fleet row there is no account on it, so the binding is what says.
        cur = ms.device_account(device_id)
        if cur is not None and cur["source"] == "admin" \
                and cur["account_id"] != principal.account_id:
            raise HTTPException(status_code=404)
    _check_pin_release(ms, body.release_id, principal)
    ms.set_device_pin(device_id, body.release_id,            # release_id=None unpins
                      account_id=principal.account_id)
    ms.append_audit(actor=principal.name, action="device.pin", entity_type="device",
                    entity_id=device_id, data={"release_id": body.release_id},
                    account_id=principal.account_id, product_id=(dev or {}).get("product_id"))
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
                    account_id=principal.account_id,
                    # None when the camera has not checked in yet: an install can be bound
                    # before the device that will fill it exists
                    product_id=(ms.get_device(device_id) or {}).get("product_id"))
    return {"device_id": device_id, "account_id": principal.account_id}


@admin.delete("/devices/{device_id}", responses={200: {"model": DeviceForgotten}})
def forget_device(device_id: str, request: Request,
                  principal: Principal = Depends(require_scope("manage"))):
    """Remove a device from the fleet: the install is gone and the camera is not coming
    back.

    This is the other half of binding one. A platform that maps each install of its
    product to a device needs a way to say an install ended -- without it a decommissioned
    camera stays in the fleet views for good and keeps consuming the account's device
    limit.

    What survives: its install history, because a deployment row records what happened on
    a day that has already passed and rollout counters are built from those rows; and the
    audit log, which is append-only and gains an entry for this.

    A camera that DOES check in again is simply a device the server has not seen before:
    it enrols from scratch. To stop one coming back, retire its registration -- this call
    is about the fleet, not about entitlement."""
    ms = request.app.state.metastore
    dev = ms.get_device(device_id)
    if dev is None or dev.get("account_id") != principal.account_id \
            or not principal.may(dev.get("product_id")):
        raise HTTPException(status_code=404)
    ms.forget_device(device_id)
    ms.append_audit(actor=principal.name, action="device.forget", entity_type="device",
                    entity_id=device_id, data={"product_id": dev.get("product_id")},
                    account_id=principal.account_id, product_id=dev.get("product_id"))
    return {"device_id": device_id, "forgotten": True}


@admin.post("/cohorts/pin", responses={200: {"model": CohortPinned}})
def pin_cohort(body: CohortPin, request: Request,
               principal: Principal = Depends(require_scope("manage"))):
    """Pin a whole cohort of a product to a release, or clear it with
    `{"release_id": null}`. A cohort pin beats a rollout but loses to a device pin, so
    a pinned cohort is a fleet-wide hold that individual devices can still be excepted
    from."""
    ms = request.app.state.metastore
    _may_product(body.product_id, principal)
    _check_pin_release(ms, body.release_id, principal)
    ms.set_cohort_pin(body.product_id, body.cohort, body.release_id,
                      account_id=principal.account_id)       # account from the token, not the body
    ms.append_audit(actor=principal.name, action="cohort.pin", entity_type="cohort",
                    entity_id=body.cohort, data={"product_id": body.product_id,
                                                 "release_id": body.release_id},
                    account_id=principal.account_id, product_id=body.product_id)
    return {"product_id": body.product_id, "product_id_str": str(body.product_id),
            "cohort": body.cohort, "release_id": body.release_id}


@admin.get("/fleet", responses={200: {"model": FleetSummary}})
def fleet(request: Request, product_id: int | None = None, cohort: str | None = None,
          totals: bool = Query(False, description="the account-wide counters alone, with an "
                                                  "empty `products` -- an overview need not "
                                                  "receive every product's breakdown"),
          principal: Principal = Depends(require_scope("observe"))):
    """The fleet summary behind a dashboard: device counts by product, how each
    product splits across versions, which release each version maps to, how many devices
    are mid-trial or fell back, and when they were last seen. `up_to_date` is the adoption
    count, per product and account-wide: devices at or past that product's OWN newest
    release (a version string cannot be compared, so the server counts it). Filter to one
    product or one cohort with the query parameters, or ask for `totals` alone."""
    from openmv_ota.ota.version import decode_app_version

    summary = request.app.state.metastore.fleet_summary(product_id,
                                                        account_id=principal.account_id,
                                                        cohort=cohort,
                                                        products=principal.scoped(),
                                                        totals=totals)
    # by_fallback is keyed by the packed uint32 the device reports; render it the way
    # by_version already reads. "unknown" is the device that did not say -- a single-image
    # board, or one on a payload from before the slots field existed.
    for prod in summary["products"].values():
        prod["by_fallback"] = {
            (decode_app_version(k) if k else "unknown"): n
            for k, n in prod["by_fallback"].items()}
    return summary


@admin.get("/activity", responses={200: {"model": ActivityList}})
def activity(request: Request, limit: int = Query(6, ge=1, le=50),
             action_not: str | None = Query(None, description="hide one action, e.g. the "
                                                              "periodic `advisory.scan`"),
             principal: Principal = Depends(require_scope("observe"))):
    """What has been happening, grouped: the newest event of each (action, actor) with
    how many times that pair appears, newest group first.

    This is what an overview wants, and a tail of `/audit` is not it: onboarding four
    hundred devices writes four hundred consecutive rows, so the newest N of anything is
    that one act repeated, with everything before it out of view no matter how deep the
    caller pages. Use `/audit` for the log itself."""
    return {"events": request.app.state.metastore.recent_activity(
        limit=limit, account_id=principal.account_id, action_not=action_not)}


@admin.get("/fleet/installs", responses={200: {"model": InstallDays}})
def fleet_installs(request: Request, days: int = Query(14, ge=1, le=90),
                   product_id: int | None = None,
                   principal: Principal = Depends(require_scope("observe"))):
    """Installs and failures per UTC day over the last `days` -- whether updates are
    actually landing, which no other read answers: `/fleet` is the fleet as it stands
    right now, and a rollout's counters are one release's story. Every day in the window
    comes back, zero included, oldest first, so the series can be drawn as given."""
    return request.app.state.metastore.installs_by_day(
        days=days, product_id=product_id, account_id=principal.account_id,
        products=principal.scoped())


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
                                                   account_id=principal.account_id,
                                                   products=principal.scoped())
    for r in rows:
        r["version"] = decode_app_version(r["payload_version"])
    return {"bases": rows}


@admin.get("/releases", responses={200: {"model": ReleaseList}})
def releases(request: Request, product_id: int | None = None, limit: int = _LIMIT_Q,
             offset: int = 0,
             sort: str | None = _sort_q("version, product, size, uploaded, name, release"),
             dir: str = _DIR_Q,
             principal: Principal = Depends(require_scope("observe"))):
    """The account's publish history, newest first. On the list contract like every
    collection: `limit`, `offset`, `sort`, `dir`, and a filter-aware `total` beside the
    rows, so a full page is never mistaken for a complete list."""
    ms = request.app.state.metastore
    return {"releases": ms.list_releases(product_id, account_id=principal.account_id,
                                         limit=limit, offset=offset, sort=sort, direction=dir,
                                         products=principal.scoped()),
            "total": ms.count_releases(product_id, principal.account_id,
                                       products=principal.scoped())}


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


@admin.get("/advisories", responses={200: {"model": AdvisoryList}})
def list_advisories(request: Request, release_id: str | None = None,
                    active_only: bool = True,
                    limit: int | None = Query(None, ge=1, le=_MAX_PAGE),
                    offset: int = 0,
                    sort: str | None = _sort_q("severity, advisory, component, release, "
                                               "first_seen, last_seen"),
                    dir: str = _DIR_Q,
                    principal: Principal = Depends(require_scope("observe"))):
    """The account's CVE findings from SBOM scans -- active by default;
    ``active_only=false`` includes cleared rows (the monitoring history)."""
    ms = request.app.state.metastore
    if release_id is not None:
        _owned(ms.get_release(release_id), principal)
    return {"advisories": ms.list_advisories(account_id=principal.account_id,
                                             release_id=release_id, active_only=active_only,
                                             sort=sort, direction=dir, limit=limit,
                                             offset=offset),
            "total": ms.count_advisories(principal.account_id, release_id, active_only)}


class AdvisoryScanRequest(BaseModel):
    release_id: str | None = None          # one release, or the whole live fleet


@admin.post("/advisories/scan", responses={200: {"model": AdvisoryScan}})
def scan_advisories(body: AdvisoryScanRequest, request: Request,
                    principal: Principal = Depends(require_scope("manage"))):
    """Run a scan NOW -- one release, or every release the fleet still runs.
    The daily scheduler calls the same code; this is the on-demand edge
    (publish-time, a dashboard button, CI)."""
    from . import advisor

    st = request.app.state
    if body.release_id is not None:
        rel = _owned(st.metastore.get_release(body.release_id), principal)
        out = advisor.scan_release(st, rel, actor=principal.name)
        return {"releases_scanned": 1, "findings": out["findings"], "new": out["new"]}
    out = advisor.scan_account(st, principal.account_id, actor=principal.name)
    return out


@admin.get("/releases/{release_id}/manifest",
           responses={200: {"content": {"application/octet-stream": {}},
                            "description": "the release's SIGNED manifest, byte-exact"}})
def release_manifest(release_id: str, request: Request,
                     principal: Principal = Depends(require_scope("observe"))):
    """Download the signed manifest exactly as published -- the root of trust a
    device verifies. Completes the artifact set for audit workflows."""
    from .errors import ServerError

    st = request.app.state
    rel = _owned(st.metastore.get_release(release_id), principal)
    try:
        data = st.storage.get(rel["manifest_key"])
    except ServerError:
        raise HTTPException(status_code=404,
                            detail="manifest is no longer retained") from None
    return Response(content=data, media_type="application/octet-stream")


@admin.get("/releases/{release_id}/artifacts/{filename}",
           responses={200: {"content": {"application/gzip": {}},
                            "description": "one artifact of the release (full image or a delta)"}})
def release_artifact(release_id: str, filename: str, request: Request,
                     principal: Principal = Depends(require_scope("observe"))):
    """Download ONE of the release's artifacts by the filename its manifest declares --
    the full image or any delta. The filename must be one of the release's declared
    representation urls (a whitelist, so this can never read outside the release's own
    artifact directory). Account-scoped like every release read."""
    from .errors import ServerError

    st = request.app.state
    rel = _owned(st.metastore.get_release(release_id), principal)
    if filename not in {r.get("url") for r in rel["representations"]}:
        raise HTTPException(status_code=404)
    try:
        data = st.storage.get("artifacts/%s/%s" % (release_id, filename))
    except ServerError:
        raise HTTPException(status_code=404,
                            detail="artifact is no longer retained") from None
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


@admin.get("/devices", responses={200: {"model": DeviceList}})
def devices(request: Request, product_id: int | None = None,
            limit: int = Query(100, ge=1, le=_MAX_PAGE),
            cohort: str | None = None, offset: int = 0,
            q: str | None = Query(None, description="name-or-id substring, case-insensitive"),
            cohort_not: str | None = Query(None, description="exclude devices in this cohort"),
            version: str | None = Query(None, description="only devices running this version"),
            older_than_release: str | None = Query(
                None, description="only devices running something older than this release "
                                  "(by payload version) -- a rollout's not-yet-updated set"),
            fell_back: bool = Query(False, description="only devices whose last boot rejected a slot"),
            unconfirmed: bool = Query(False, description="only devices mid-trial (install unconfirmed)"),
            not_seen_since: float | None = Query(
                None, description="only devices with no check-in since this epoch second"),
            seen_since: float | None = Query(
                None, description="only devices that HAVE checked in since this epoch second "
                                  "-- the exact complement of not_seen_since"),
            behind: bool = Query(False, description="only devices with a newer release "
                                                    "published for their own product"),
            up_to_date: bool = Query(False, description="only devices at or past their own "
                                                        "product's newest release -- the "
                                                        "complement of behind among devices "
                                                        "whose product has published one"),
            installed_on: str | None = Query(None, description="only devices whose deployment "
                                                               "was last reported installed on "
                                                               "this UTC day (YYYY-MM-DD) -- one "
                                                               "column of /fleet/installs"),
            failed_on: str | None = Query(None, description="the same for a reported failure"),
            sort: str | None = _sort_q("seen, device, product, version, cohort, first_seen"),
            dir: str = _DIR_Q,
            principal: Principal = Depends(require_scope("observe"))):
    """Every device in the account, with what it is running and when it last checked
    in. On the list contract (`limit`, `offset`, `sort`, `dir`, `total`), plus the
    filters a fleet view actually needs: `product_id` and `cohort` to narrow,
    `version` and `older_than_release` to find what is behind, `fell_back` and
    `unconfirmed` for devices that need attention, `not_seen_since` / `seen_since` (an
    epoch second) for the ones that have gone quiet, or are alive, and `behind` /
    `up_to_date` for the two halves of the adoption the fleet summary counts, and
    `installed_on` / `failed_on` for one day of the install series as a list."""
    ms = request.app.state.metastore
    older_pv = None
    if older_than_release is not None:
        older_pv = _owned(ms.get_release(older_than_release), principal)["payload_version"]
    return {"devices": _with_fallback_version(ms.list_devices(
                product_id, limit, account_id=principal.account_id, cohort=cohort, offset=offset,
                sort=sort, direction=dir, q=q, cohort_not=cohort_not, version=version,
                older_than_pv=older_pv, fell_back=fell_back or None, unconfirmed=unconfirmed or None,
                not_seen_since=not_seen_since, products=principal.scoped(),
                seen_since=seen_since, behind=behind or None,
                up_to_date=up_to_date or None, installed_on=installed_on,
                failed_on=failed_on)),
            "total": ms.count_devices(product_id, principal.account_id, cohort, q, cohort_not,
                                      version, older_pv, fell_back or None, unconfirmed or None,
                                      not_seen_since, products=principal.scoped(),
                                      seen_since=seen_since, behind=behind or None,
                                      up_to_date=up_to_date or None,
                                      installed_on=installed_on, failed_on=failed_on)}


@admin.get("/products", responses={200: {"model": ProductList}})
def products(request: Request, limit: int | None = Query(None, ge=1, le=_MAX_PAGE),
             offset: int = 0,
             sort: str | None = _sort_q("product, devices, releases, newest"), dir: str = _DIR_Q,
             principal: Principal = Depends(require_scope("observe"))):
    """The account's product directory: every product id seen on a device or a
    release, its friendly name and newest version (from the newest release), and
    device / release counts. On the list contract like every collection."""
    rows, total = request.app.state.metastore.page_products(
        account_id=principal.account_id, sort=sort, direction=dir, limit=limit, offset=offset,
        products=principal.scoped())
    return {"products": rows, "total": total}


@admin.get("/products/{product_id}", responses={200: {"model": Product}})
def get_product(product_id: int, request: Request,
                principal: Principal = Depends(require_scope("observe"))):
    """One product's directory row (label, newest version, counts). 404 for a product
    the account has never seen, or one outside a product-scoped token."""
    _may_product(product_id, principal)
    rows, _ = request.app.state.metastore.page_products(
        account_id=principal.account_id, products=principal.scoped())
    row = next((r for r in rows if r["product_id"] == product_id), None)
    if row is None:
        raise HTTPException(status_code=404)
    return row


class ProductDeclare(BaseModel):
    product_id: int
    """The id from the project's own config (`ota.toml`), which is where it is computed:
    the low 63 bits of sha256("<product>:<board>"). The server does not derive it, so the
    project stays the one place a product is named."""
    display_name: str = ""


@admin.post("/products", responses={200: {"model": ProductDeclared}})
def declare_product(body: ProductDeclare, request: Request,
                    principal: Principal = Depends(require_scope("manage"))):
    """Declare a product for this account before anything has been published to it.

    A product used to come into existence only as a side effect of publishing, which is
    the wrong order for a platform: it wants to create the project, name it, and bind its
    first cameras -- and only then build and publish an image for them. A declared
    product appears in `GET /api/v1/admin/products` with no releases and no devices.

    Idempotent: declaring a product that already exists sets its display name (when one
    is given) and answers `created: false`."""
    ms = request.app.state.metastore
    _may_product(body.product_id, principal)
    known = any(p["product_id"] == body.product_id
                for p in ms.list_products(account_id=principal.account_id))
    name = _label(body.display_name)
    if not known or name:
        ms.set_product_name(body.product_id, name, account_id=principal.account_id)
    if not known:
        ms.append_audit(actor=principal.name, action="product.create", entity_type="product",
                        entity_id=str(body.product_id), data={"name": name},
                        account_id=principal.account_id, product_id=body.product_id)
    return {"product_id": body.product_id, "product_id_str": str(body.product_id),
            "display_name": name, "created": not known}


@admin.patch("/products/{product_id}/name", responses={200: {"model": ProductRenamed}})
def rename_product(product_id: int, body: DeviceName, request: Request,
                   principal: Principal = Depends(require_scope("manage"))):
    """Set a product's display name -- a label for dashboards and lists; the product
    id stays the identity and the manifest's own name shows again when cleared ('').
    A product the account has never seen (no device, no release) is a 404."""
    name = _label(body.name)
    ms = request.app.state.metastore
    _may_product(product_id, principal)
    if not any(p["product_id"] == product_id
               for p in ms.list_products(account_id=principal.account_id)):
        raise HTTPException(status_code=404)
    ms.set_product_name(product_id, name, account_id=principal.account_id)
    ms.append_audit(actor=principal.name, action="product.rename", entity_type="product",
                    entity_id=str(product_id), data={"name": name},
                    account_id=principal.account_id, product_id=product_id)
    return {"product_id": product_id, "product_id_str": str(product_id), "display_name": name}


@admin.post("/products/{product_id}/viewer-grant", responses={200: {"model": ProductViewerGrant}})
def product_viewer_grant(product_id: int, request: Request,
                         principal: Principal = Depends(require_scope("observe"))):
    """Mint a short-lived read credential for a product's data across ALL its devices:
    the datalake's product viewer token and the URLs it opens (topics, series). The
    product must be one of the account's (seen on a device or a release), else 404;
    a server with no datalake answers 503."""
    st = request.app.state
    _may_product(product_id, principal)
    if not any(p["product_id"] == product_id
               for p in st.metastore.list_products(account_id=principal.account_id)):
        raise HTTPException(status_code=404)
    grant = datalog_mod.product_grant(st.settings, principal.account_id, product_id)
    if grant is None:
        raise HTTPException(status_code=503, detail="the datalake is not configured")
    return grant


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
    grant = _viewer_grant_for(request.app.state, principal, device_id)
    if grant is None:
        raise HTTPException(status_code=404)
    if not grant:
        raise HTTPException(status_code=503, detail="live/viewing is not configured")
    return grant


def _viewer_grant_for(st, principal, device_id: str):
    """One device's viewer grant, or None when the device is not this credential's to
    view (missing, or bound elsewhere -- indistinguishable on purpose), or ``{}`` when
    live/viewing is not configured on this deployment at all."""
    ms = st.metastore
    device = ms.get_device(device_id)
    if device is None:
        return None
    bound = ms.device_account(device_id)          # the sticky binding wins
    owner = bound["account_id"] if bound else device.get("account_id", "")
    if owner != principal.account_id or not principal.may(device.get("product_id")):
        return None
    grant = live_mod.viewer_grant(
        st.settings, device_id, (device.get("streams") or "").split(","),
        datalake_url=getattr(st.settings, "datalake_url", "") or "")
    return grant if grant is not None else {}


class ViewerGrantsRequest(BaseModel):
    device_ids: list[str]


_VIEWER_GRANTS_MAX = 100


@admin.post("/devices/viewer-grants", responses={200: {"model": ViewerGrants}})
def viewer_grants(body: ViewerGrantsRequest, request: Request,
                  principal: Principal = Depends(require_scope("observe"))):
    """Viewer grants for a page of devices in one call -- a dashboard drawing a hundred
    tiles should not have to make a hundred round trips to mint a hundred credentials.

    Each entry is exactly what the single-device call returns, or ``null`` for a device
    this credential may not view (missing, bound to another account, outside a limited
    token's products -- the page still renders, with that tile empty). At most 100 ids,
    which is a page; 400 above that. 503 only when live/viewing is not configured at all.
    """
    if len(body.device_ids) > _VIEWER_GRANTS_MAX:
        raise HTTPException(status_code=400,
                            detail="at most %d device ids per call" % _VIEWER_GRANTS_MAX)
    st = request.app.state
    if not (st.settings.live_relay_url and st.settings.live_token_secret) \
            and not (getattr(st.settings, "datalake_url", "") and st.settings.datalake_token_secret):
        raise HTTPException(status_code=503, detail="live/viewing is not configured")
    return {"grants": {did: (_viewer_grant_for(st, principal, did) or None)
                       for did in dict.fromkeys(body.device_ids)}}


@admin.get("/audit", responses={200: {"model": AuditList}})
def audit(request: Request, since: int = 0,
          limit: int = Query(100, ge=1, le=_MAX_PAGE), offset: int = 0,
          entity_id: str | None = None, newest: bool = False,
          action: str | None = Query(None, description="only this action, e.g. device.refused"),
          action_not: str | None = Query(None, description="hide one action, e.g. advisory.scan"),
          sort: str | None = _sort_q("when, action, actor, entity"), dir: str = _DIR_Q,
          account_id: str | None = Query(None, description="(server root only) one account's log"),
          all_accounts: bool = Query(False, alias="all",
                                     description="(server root only) every account's log"),
          principal: Principal = Depends(require_scope("observe"))):
    """The append-only record. ``entity_id`` narrows it to one release, rollout,
    or device -- a dashboard's per-entity history. ``newest`` returns the most
    recent events first (otherwise: append order from ``since``, a log tail);
    ``sort``/``dir`` generalise both, ``offset`` pages, ``total`` counts the filter.

    A product-limited credential reads the history of ITS products: the entries that
    happened to one of them, and not the account-level ones (tokens, billing, other
    products) that belong to whoever owns the account rather than to its customer."""
    ms = request.app.state.metastore
    # `scoped()` is None for an ordinary token (the whole account) and a list for a
    # limited one -- deliberately not the empty list for the unlimited case, where empty
    # would mean "allowed nothing".
    products = principal.scoped()
    scope = principal.account_id
    if ACCOUNT_ROOT in principal.scopes:
        # The server's root reads any account's log by naming it, or every account's at
        # once with `all` -- the operator's console, and no one else's: an account
        # credential's `account_id` is its own, whatever it puts in the query.
        scope = None if all_accounts else (account_id or principal.account_id or None)
    return {"events": ms.read_audit(limit, since, account_id=scope,
                                    entity_id=entity_id, newest=newest, sort=sort,
                                    direction=dir, offset=offset, action_not=action_not,
                                    action=action, products=products),
            "total": ms.count_audit(since, scope, entity_id, action_not, action,
                                    products=products)}

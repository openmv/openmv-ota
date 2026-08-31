"""Response schemas for the admin + device API — **documentation, not enforcement**.

Every operation used to describe its 200 as an untyped object, so ``/openapi.json`` said
nothing about what comes back: ``/docs`` showed request bodies and blank responses, and anyone
generating a client from the spec got ``Any`` from every call. These models fill that in.

**They are attached with ``responses={200: {"model": X}}``, deliberately NOT ``response_model``.**
That distinction is the whole design here:

* ``response_model`` *filters* — FastAPI drops any field the model does not declare. Our rows
  come back from ``SELECT *`` through the metastore, so a model that lags a migration by one
  column would make that column **silently vanish from the API**. Silent truncation is exactly
  the failure this API has been burned by before, and a schema is not worth introducing it.
* ``responses={200: ...}`` only *documents*. The wire bytes are untouched, so a field this file
  forgets is still delivered; the cost of being wrong here is an incomplete doc, not lost data.

So: rows below are ``extra="allow"`` and their fields optional. The models describe the shape a
consumer can rely on, and stay quiet about anything they do not know. When the shapes have
settled and there is a test proving model-vs-store parity, tightening to ``response_model`` is
a deliberate follow-up — not something to drift into.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class _Row(BaseModel):
    """A store row. Open by construction — see the module docstring."""

    model_config = ConfigDict(extra="allow")


# --- rows -------------------------------------------------------------------------------------

class Account(_Row):
    account_id: str = ""
    name: str = ""
    created_at: str = ""
    active: int = 1


class TokenInfo(_Row):
    """A token's METADATA. The secret itself is returned exactly once, by issue/rotate."""

    token_hash: str = ""
    name: str = ""
    scopes: str | list[str] = ""
    created_at: str = ""
    revoked: int = 0
    account_id: str = ""


class Release(_Row):
    release_id: str = ""
    product_id: int = 0
    product: str = ""
    version: str = ""
    payload_version: int = 0
    min_platform_version: int = 0
    image_sha256: str = ""
    image_size: int = 0
    representations: Any = None
    key_id: int | None = None
    uploaded_by: str = ""
    uploaded_at: str = ""
    account_id: str = ""
    dev: int = 0


class Rollout(_Row):
    rollout_id: str = ""
    release_id: str = ""
    product_id: int = 0
    cohort: str = ""
    percent: float = 0.0
    cohort_devices: int = 0
    """Devices in this rollout's (product, cohort) right now -- the audience its percent
    applies to. Computed live on list reads; cohort membership shifts under the rollout."""
    state: str = ""
    failure_threshold: float = 0.0
    attempted: int = 0
    updated: int = 0
    failures: int = 0
    created_at: str = ""
    updated_at: str = ""
    account_id: str = ""


class Device(_Row):
    device_id: str = ""
    product_id: int = 0
    board: str = ""
    cohort: str = ""
    current_version: str = ""
    current_payload_version: int = 0
    slot: str = ""
    representation: str = ""
    fallback_reason: str | None = None
    confirmed: int = 0
    last_offered_release_id: str | None = None
    first_seen: str = ""
    last_seen: str = ""
    pinned_release_id: str | None = None
    account_id: str = ""
    fallback_payload_version: int | None = None
    fallback_version: str | None = None
    """Decoded from ``fallback_payload_version`` by the API, so a reader need not unpack the
    uint32 the device reports. ``null`` when the device did not say — deliberately distinct
    from a device that reported no fallback."""
    body_sha256: str | None = None
    """The RUNNING slot's exact bytes (its trailer's body_sha256, hex) as the device reported
    them. A delta base matches by version AND these bytes; ``null`` = the device did not say."""


class FleetBase(_Row):
    payload_version: int = 0
    version: str = ""
    """Decoded from ``payload_version`` by the API."""
    body_sha256: str = ""
    """"" = devices that did not report a sha (pre-sha payloads) — they can only take full
    images, so a delta plan need not cover them."""
    devices: int = 0


class FleetBases(BaseModel):
    bases: list[FleetBase] = []


class Cohort(_Row):
    cohort: str = ""
    devices: int = 0


class AuditEvent(_Row):
    seq: int = 0
    ts: str = ""
    actor: str = ""
    action: str = ""
    entity_type: str = ""
    entity_id: str = ""
    data: Any = None
    prev_hash: str = ""
    entry_hash: str = ""
    account_id: str = ""


# --- collections ------------------------------------------------------------------------------
# `total` is the count the page was drawn from, so a FULL page can be told apart from a
# TRUNCATED list; it is account-scoped like the rows, and never discloses another tenant's size.

class AccountList(BaseModel):
    accounts: list[Account]


class TokenList(BaseModel):
    tokens: list[TokenInfo]


class ReleaseList(BaseModel):
    releases: list[Release]
    total: int


class RolloutList(BaseModel):
    rollouts: list[Rollout]
    total: int


class DeviceList(BaseModel):
    devices: list[Device]
    total: int


class CohortList(BaseModel):
    cohorts: list[Cohort]


class AuditList(BaseModel):
    events: list[AuditEvent]


# --- summaries + action results -----------------------------------------------------------------

class FleetSummary(BaseModel):
    """Fleet EXPOSURE, not slot occupancy: under A/B the slot a device runs from alternates, so
    which one it is says nothing useful. ``by_fallback`` is keyed by decoded version, with
    ``"unknown"`` for a device that did not report one."""

    total: int
    by_version: dict[str, int]
    by_fallback: dict[str, int]
    fell_back: int
    unconfirmed: int


class AccountCreated(BaseModel):
    account_id: str
    name: str
    token: str
    """The account's first admin token. Returned ONCE, here — it is not recoverable."""


class AccountNamed(BaseModel):
    account_id: str
    name: str


class AccountActive(_Row):
    account_id: str
    active: bool | int
    tokens_revoked: int | None = None
    """Present on deactivate: how many of the account's tokens were revoked with it."""


class TokenIssued(BaseModel):
    token_hash: str
    name: str
    scopes: list[str]
    account_id: str
    token: str
    """The secret. Returned ONCE, here — store it now; only its hash is kept."""


class TokenRevoked(BaseModel):
    token_hash: str
    revoked: bool


class RolloutCreated(BaseModel):
    rollout_id: str
    product_id: int
    cohort: str
    percent: float
    state: str


class RolloutState(BaseModel):
    rollout_id: str
    state: str


class RolloutStatus(BaseModel):
    rollout_id: str
    state: str
    percent: float
    cohort_devices: int = 0
    """Devices in the rollout's (product, cohort) right now -- its current audience."""
    staged_devices: int = 0
    """The current target: ``round(cohort_devices * percent / 100)``. An estimate --
    membership is a hash, not a list, so the true staged count varies around it."""
    attempted: int
    updated: int
    failures: int
    rates: dict[str, float] | None = None
    """Each counter as a fraction of ``staged_devices`` (keys ``attempted`` /
    ``updated`` / ``failures``); null until anything is staged."""
    reported: dict[str, int]
    """Explicit device reports (``POST /feedback``) for this rollout's release."""


class CohortAssigned(BaseModel):
    cohort: str
    assigned: int


class DevicePinned(BaseModel):
    device_id: str
    pinned_release_id: str | None = None


class DeviceBound(BaseModel):
    device_id: str
    account_id: str


class CohortPinned(BaseModel):
    product_id: int
    cohort: str
    release_id: str | None = None


class ArtifactsDeleted(BaseModel):
    release_id: str
    deleted: list[str] | int


class Published(BaseModel):
    release_id: str
    product_id: int
    version: str | None = None
    payload_version: int
    representations: list[str]


class ViewerGrant(_Row):
    """A short-lived viewer token plus the URLs it opens. Watch-only: it can never publish
    frames or ingest data (see ``live.camera_grant`` for the asymmetry)."""

    token: str = ""
    streams: dict[str, dict[str, str]] = {}
    expires_in_s: int = 0
    topics_url: str | None = None
    logs_url: str | None = None
    series_url: str | None = None


# --- device-facing ------------------------------------------------------------------------------

class CheckAnswer(_Row):
    """The check-in answer. ``update`` false is the common case and carries nothing else."""

    update: bool = False
    manifest_url: str | None = None
    release_id: str | None = None
    poll_after_s: int | None = None


class Ok(BaseModel):
    ok: bool


class Health(BaseModel):
    ok: bool

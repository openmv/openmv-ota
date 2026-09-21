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
    device_limit: int | None = None
    client_ref: str | None = None
    devices: int = 0                 # registered devices
    releases: int = 0
    active_rollouts: int = 0
    last_seen: str | None = None     # the newest device check-in, or None
    """Max registered devices (an entitlement set by the operator); null = unlimited."""


class TokenInfo(_Row):
    """A token's METADATA. The secret itself is returned exactly once, by issue/rotate."""

    token_hash: str = ""
    name: str = ""
    scopes: str | list[str] = ""
    products: list[int] = []
    """The product ids this token is limited to; empty is the whole account."""
    created_at: str = ""
    revoked: int = 0
    account_id: str = ""


class Release(_Row):
    release_id: str = ""
    product_id: int = 0
    product_id_str: str = ""
    """The same id as a string. JSON numbers are doubles in JavaScript, so a 63-bit
    id loses precision in JSON.parse -- silently. Read this one from JS."""
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
    display_name: str = ""


class Rollout(_Row):
    rollout_id: str = ""
    release_id: str = ""
    product_id: int = 0
    product_id_str: str = ""
    """The same id as a string. JSON numbers are doubles in JavaScript, so a 63-bit
    id loses precision in JSON.parse -- silently. Read this one from JS."""
    cohort: str = ""
    percent: float = 0.0
    cohort_devices: int = 0
    up_to_date: int = 0
    """Of those, the devices running this rollout's release or newer -- its real progress."""
    pause_reason: str | None = None
    """Why it is paused: ``operator``, ``superseded`` (a newer rollout took its cohort) or
    ``failure_limit`` (auto-paused); None while active or stopped."""
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
    product_id_str: str = ""
    """The same id as a string. JSON numbers are doubles in JavaScript, so a 63-bit
    id loses precision in JSON.parse -- silently. Read this one from JS."""
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
    publish_seq: int = 0
    """The running image's account publish counter, as the camera last reported it."""
    orders_by_seq: int = 0
    """1 when this camera orders images by ``publish_seq`` rather than ``payload_version``
    -- its firmware's product_id is 0, so it can be moved between products."""
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


class InstallDay(BaseModel):
    """One UTC day of the install series. Every day in the window is present, zero
    included, so a caller draws the series without inventing the gaps."""

    day: str = ""
    """The UTC date, ``YYYY-MM-DD``."""
    installed: int = 0
    failed: int = 0


class InstallDays(BaseModel):
    days: list[InstallDay] = []
    """Oldest first."""
    installed: int = 0
    failed: int = 0
    """Totals over the whole window."""


class Cohort(_Row):
    cohort: str = ""
    devices: int = 0
    by_product: dict[str, int] = {}
    """Device counts per product id within the cohort (JSON object keys are strings) --
    a cohort name spans products, and targeting is always (product, cohort)."""
    pins: dict[str, str] = {}
    """Release id the cohort is pinned to, per product id -- devices there stay on it
    and rollouts don't move them. Absent product = not pinned."""


class ActivityEvent(_Row):
    """The newest event of one (action, actor) pair, with how many times that pair
    appears in the log. An overview draws a fixed number of these; a burst of one act
    is one row carrying its count, not the whole panel."""

    seq: int = 0
    ts: str = ""
    actor: str = ""
    action: str = ""
    entity_type: str = ""
    entity_id: str = ""
    data: Any = None
    count: int = 0
    """Occurrences of this (action, actor) in the log."""


class ActivityList(BaseModel):
    events: list[ActivityEvent] = []
    """Newest group first."""


class AuditEvent(_Row):
    seq: int = 0
    ts: str = ""
    actor: str = ""
    action: str = ""
    entity_type: str = ""
    entity_id: str = ""
    data: Any = None
    product_id: int | None = None
    """The product this happened to, where there is one -- account-level acts (tokens,
    limits) have none. It is what a product-limited credential's history is filtered by,
    and it sits beside the hash chain rather than inside it: the chain covers what the
    entry asserts, and this is an index onto the same act."""
    prev_hash: str = ""
    entry_hash: str = ""
    account_id: str = ""


# --- collections ------------------------------------------------------------------------------
# `total` is the count the page was drawn from, so a FULL page can be told apart from a
# TRUNCATED list; it is account-scoped like the rows, and never discloses another tenant's size.

class AccountLimited(BaseModel):
    account_id: str
    device_limit: int | None = None
    devices: int = 0
    """Registered devices right now, so the caller sees headroom (or the overrun)."""


class AccountList(BaseModel):
    accounts: list[Account]
    total: int                       # what the filter matched, not the page


class TokenList(BaseModel):
    tokens: list[TokenInfo]


class ReleaseList(BaseModel):
    releases: list[Release]
    total: int


class RolloutRow(_Row):
    """A list row: just enough to find and recognize a rollout. Everything else --
    policy, timestamps, counters, the derived score -- is the /status read."""

    rollout_id: str = ""
    release_id: str = ""
    product_id: int = 0
    product_id_str: str = ""
    """The same id as a string. JSON numbers are doubles in JavaScript, so a 63-bit
    id loses precision in JSON.parse -- silently. Read this one from JS."""
    cohort: str = ""
    percent: float = 0.0
    state: str = ""
    cohort_devices: int = 0


class RolloutList(BaseModel):
    rollouts: list[RolloutRow]
    total: int


class DeviceList(BaseModel):
    devices: list[Device]
    total: int


class CohortList(BaseModel):
    cohorts: list[Cohort]
    total: int = 0


class AuditList(BaseModel):
    events: list[AuditEvent]
    total: int = 0


# --- summaries + action results -----------------------------------------------------------------

class ProductFleet(BaseModel):
    """One product's slice of the fleet. ``by_fallback`` is keyed by decoded version,
    with ``"unknown"`` for a device that did not report one (a single-image board)."""

    total: int
    by_version: dict[str, int]
    by_fallback: dict[str, int]
    by_cohort: dict[str, int]
    releases: dict[str, dict] = {}
    """version string -> ``{release_id, display_name}`` for the product's published
    releases (newest when a version was republished): links a running version to its release."""
    fell_back: int
    unconfirmed: int
    up_to_date: int
    """Devices at or past this product's newest release."""
    measured: int
    """The devices that count toward that: `total` once anything is published, 0 while
    nothing is (with no newest release, a device is outside adoption, not behind it)."""


class FleetSummary(BaseModel):
    """The fleet, structured per product (version strings and cohort composition only
    mean anything within one product). The top level carries the account-wide alarms;
    ``products`` is keyed by product id (JSON object keys are strings)."""

    total: int
    fell_back: int
    unconfirmed: int
    up_to_date: int
    """Account-wide adoption: devices at or past their own product's newest release."""
    measured: int
    """Adoption's denominator: the devices whose product HAS a newest release. Devices in
    a product with nothing published are outside the ratio, never 0% of it."""
    products: dict[str, ProductFleet]
    """Empty when the read asked for `totals`: the counters above are all it returns."""


class AccountCreated(BaseModel):
    account_id: str
    name: str
    token: str | None
    """The account's first admin token. Returned ONCE, here — it is not recoverable.
    ``null`` on a repeated create (see ``created``): the token was handed over on the
    call that made the account, and minting a second one on a retry would leave a live
    credential nobody is tracking."""
    created: bool = True
    """False when this call matched an existing ``client_ref`` and returned that account
    instead of making another."""
    client_ref: str = ""


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
    product_id_str: str = ""
    """The same id as a string. JSON numbers are doubles in JavaScript, so a 63-bit
    id loses precision in JSON.parse -- silently. Read this one from JS."""
    cohort: str
    percent: float
    state: str


class RolloutState(BaseModel):
    rollout_id: str
    state: str


class RolloutStatus(BaseModel):
    """The complete single-rollout read: the stored row plus the derived score."""

    rollout_id: str
    release_id: str = ""
    product_id: int = 0
    product_id_str: str = ""
    """The same id as a string. JSON numbers are doubles in JavaScript, so a 63-bit
    id loses precision in JSON.parse -- silently. Read this one from JS."""
    cohort: str = ""
    state: str
    percent: float
    failure_threshold: float = 0.0
    created_at: str = ""
    updated_at: str = ""
    account_id: str = ""
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


class CohortDeleted(BaseModel):
    cohort: str
    devices: int
    """Devices returned to __default__."""
    pins: int


class CohortCreated(BaseModel):
    cohort: str
    """The declared label, empty until its first assign."""


class CohortRenamed(BaseModel):
    cohort: str
    """The new label."""
    renamed_from: str
    devices: int
    rollouts: int
    pins: int


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
    product_id_str: str = ""
    """The same id as a string. JSON numbers are doubles in JavaScript, so a 63-bit
    id loses precision in JSON.parse -- silently. Read this one from JS."""
    cohort: str
    release_id: str | None = None


class Advisory(_Row):
    release_id: str = ""
    vuln_id: str = ""
    component: str = ""
    version: str = ""
    severity: str = "unknown"
    summary: str = ""
    first_seen: str = ""
    last_seen: str = ""
    cleared_at: str | None = None
    account_id: str = ""
    release_name: str = ""
    """The release's display name ('' when unnamed) -- a label without a second lookup."""


class AdvisoryList(BaseModel):
    advisories: list[Advisory]
    total: int = 0


class Product(BaseModel):
    product_id: int
    product_id_str: str = ""
    """The same id as a string. JSON numbers are doubles in JavaScript, so a 63-bit
    id loses precision in JSON.parse -- silently. Read this one from JS."""
    product: str | None = None
    """Friendly name from the newest release; None until one is published."""
    """The label to show: the operator's display name, else the newest release's
    manifest name; None until either exists."""
    display_name: str = ""
    """The operator's own label ('' = none set)."""
    manifest_name: str | None = None
    """The product name the newest release's manifest carries."""
    devices: int = 0
    releases: int = 0
    newest_version: str | None = None
    """The newest release's version string; None until one is published."""
    newest_payload_version: int | None = None
    up_to_date: int = 0
    """Devices running the newest release or past it -- the product's adoption in one figure."""


class ProductList(BaseModel):
    products: list[Product]
    total: int = 0


class ProductDeclared(BaseModel):
    product_id: int
    product_id_str: str = ""
    """The same id as a string. JSON numbers are doubles in JavaScript, so a 63-bit
    id loses precision in JSON.parse -- silently. Read this one from JS."""
    display_name: str
    created: bool = True
    """False when the product was already known to this account."""


class Webhook(BaseModel):
    webhook_id: str
    url: str
    events: list[str]
    """Subscribed event types: exact (`rollout.stop`), a family (`rollout.*`) or `*`."""
    active: int = 1
    description: str = ""
    created_at: str = ""
    created_by: str = ""
    failures: int = 0
    """Consecutive failed attempts; 50 disables the endpoint."""
    disabled_reason: str = ""
    last_delivery_at: str | None = None
    last_status: int | None = None


class WebhookCreated(Webhook):
    secret: str
    """Shown once. Verify `X-OpenMV-Signature` (`t=<ts>,v1=<hex>`) as HMAC-SHA256 of
    `<ts>.<body>` under it."""


class WebhookList(BaseModel):
    webhooks: list[Webhook]
    events: dict[str, str]
    """The event catalogue: every type a subscription may name, with what it means."""


class WebhookDeleted(BaseModel):
    webhook_id: str
    deleted: bool = True


class WebhookPinged(BaseModel):
    webhook_id: str
    delivery_id: str
    """A `webhook.ping` event was queued; watch it in the deliveries list."""


class Delivery(BaseModel):
    delivery_id: str
    webhook_id: str
    audit_seq: int
    event: str
    status: str
    """pending (queued or awaiting a retry), delivered, dead (every attempt failed)."""
    attempt: int
    next_at: float
    last_code: int | None = None
    last_error: str = ""
    created_at: str
    delivered_at: str | None = None


class DeliveryList(BaseModel):
    deliveries: list[Delivery]
    total: int


class DeviceForgotten(BaseModel):
    device_id: str
    forgotten: bool = True
    data_deleted: int | None = None
    """Objects the datalake erased with it; None when the data was kept (``keep_data``)
    or the server has no datalake."""
    data_bytes: int | None = None


class ProductRenamed(BaseModel):
    product_id: int
    product_id_str: str = ""
    """The same id as a string. JSON numbers are doubles in JavaScript, so a 63-bit
    id loses precision in JSON.parse -- silently. Read this one from JS."""
    display_name: str


class AdvisoryScan(BaseModel):
    releases_scanned: int
    findings: int
    new: list[Advisory]


class Published(BaseModel):
    release_id: str
    product_id: int
    product_id_str: str = ""
    """The same id as a string. JSON numbers are doubles in JavaScript, so a 63-bit
    id loses precision in JSON.parse -- silently. Read this one from JS."""
    version: str | None = None
    payload_version: int
    representations: list[str]
    display_name: str = ""


class ViewerGrant(_Row):
    """A short-lived viewer token plus the URLs it opens. Watch-only: it can never publish
    frames or ingest data (see ``live.camera_grant`` for the asymmetry). ``token`` opens
    the relay's watch URLs; the datalake read endpoints ride their OWN token under
    ``datalake`` -- the two services sign with separate secrets."""

    token: str = ""
    streams: dict[str, dict[str, str]] = {}
    expires_in_s: int = 0
    datalake: dict | None = None
    """``{token, topics_url, logs_url, series_url, expires_in_s}`` when the datalake is
    configured; absent otherwise."""


class ViewerGrants(BaseModel):
    grants: dict[str, ViewerGrant | None] = {}
    """One entry per requested device id: its grant, or ``null`` when this credential may
    not view it (missing, another account's, or outside a limited token's products)."""


class ProductViewerGrant(BaseModel):
    """A short-lived read credential for one product's data across all its devices:
    the datalake's product ``viewer`` token under ``datalake`` with the URLs it opens
    (``topics_url``; ``series_url`` + ``/{topic}``)."""

    datalake: dict
    expires_in_s: int = 0


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
    commit: str = ""        # the deployment's build commit, when the host provides one

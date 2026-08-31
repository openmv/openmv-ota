# The server API

*[← 16 · The update server](16-update-server.md) · [Index](00-introduction.md)*

---

Everything the `client` verb does is a call to this HTTP API — so anything the CLI can
do, your scripts, CI, and dashboards can do too. The server documents itself: `/docs`
serves a browsable reference and `/openapi.json` the machine-readable schema, so a client
can be generated instead of hand-written against guesses.

Two audiences, two auth models:

- **Device endpoints** (`/api/v1/check`, `/api/v1/feedback`, `/d/…`) are called by
  cameras. No credentials — they are rate-limited per IP and gated by device
  registration, and downloads are authorized by expiring capability tokens.
- **Admin endpoints** (`/api/v1/admin/…`) take `Authorization: Bearer <token>`. A token
  belongs to an **account** and carries **scopes** (`publish`, `manage`, `observe`,
  `accounts`); every read and write is scoped to the token's account, and anything
  belonging to another account answers **404** — indistinguishable from "doesn't exist",
  so the API can't be used to probe other tenants.

## The device API

**`POST /api/v1/check`** — the check-in. The device runtime sends its identity and state:

| field | meaning |
|---|---|
| `device_id`, `board` | the registration pair — the MCU's unique id + the board name |
| `product_id`, `account_id` | which product/tenant this camera belongs to (baked into its firmware) |
| `app_version`, `payload_version` | the running release, human and packed forms |
| `slot`, `representation`, `confirmed`, `fallback_reason` | trial state: which slot booted, how it was installed, whether it confirmed, why the other slot was rejected (if it was) |
| `slots` | every slot, newest first — what the device would fall back to |
| `streams` | live image stream names (multi-camera boards); empty means the single default |

The answer is `{"update": false, "poll_after_s": 3600}` in the common case. On an offer:

```json
{
  "update": true,
  "manifest_url": "https://ota.cloud.openmv.io/d/<token>/manifest.bin",
  "release_id": "rel_4f9c2a81d06b73ee",
  "poll_after_s": 3600
}
```

Deployments wired for OpenMV's live-viewing or data-ingest services add `live` / `ingest`
grants to the same answer. Over the rate limit, the reply is `429` with a `Retry-After`
header.

**`GET /d/{token}/{filename}`** — the capability gateway. The token is a signed, expiring
credential minted only when a registered device is offered a release, and **one token
authorizes the whole bundle**: the manifest and every image/delta beside it resolve under
the same `/d/{token}/` prefix, which is why the signed manifest's artifact URLs are
relative filenames. A filename must match something the signed manifest declares —
the token can't be used to fish for other stored objects. On s3 storage the response is a
`302` to a short-lived presigned URL (bandwidth offloads to object storage); on local
storage the bytes stream directly, honouring single-range `Range` requests so a device on
a poor link can resume an interrupted download instead of restarting it.

**`POST /api/v1/feedback`** — the explicit terminal outcome of an offered update:
`device_id`, `board`, `product_id`, `release_id`, and `status` (`installed` or `failed`,
optionally a `reason`). Recorded only for registered devices; these reports are the
`reported` counts in a rollout's status.

**`GET /healthz`** — liveness: `{"ok": true}`.

## The admin API

Grouped by what they manage — the scope column is what the bearer token must carry:

**Publishing**

| endpoint | scope | what it does |
|---|---|---|
| `POST /api/v1/admin/releases` | publish | multipart upload: `manifest` + `image` + repeated `delta` parts + optional `sbom`. All metadata derives from the signed manifest; inconsistent artifacts are refused (400); the manifest's `account_id` must match the token's (403); `payload_version` at or below the newest published is refused (409) unless `?allow_republish=true` |
| `GET /api/v1/admin/releases` | observe | the publish history (paged) |
| `GET /api/v1/admin/releases/{id}` | observe | one release |
| `GET /api/v1/admin/releases/{id}/image` | observe | the retained image bytes — the delta-base download. A release whose bytes were pruned answers "image is no longer retained" |
| `GET /api/v1/admin/releases/{id}/sbom` | observe | the release's CycloneDX SBOM, as uploaded |
| `DELETE /api/v1/admin/releases/{id}/artifacts` | publish | prune the stored objects, keep the row; 409 while an active rollout offers it, `?force=true` overrides |

**Rollouts**

| endpoint | scope | what it does |
|---|---|---|
| `POST /api/v1/admin/rollouts` | manage | create (release, cohort, percent, optional `failure_threshold`, default 0.05). Supersedes — pauses — the cohort's prior active rollout |
| `PATCH /api/v1/admin/rollouts/{id}` | manage | raise `percent` (monotonic — lowering is 400) and/or set `state` to `active`/`paused` |
| `POST /api/v1/admin/rollouts/{id}/rollback` | manage | terminal stop; devices that took the release keep it |
| `GET /api/v1/admin/rollouts` | observe | list (paged) |
| `GET /api/v1/admin/rollouts/{id}/status` | observe | the counters: `attempted`, `updated`, `failures`, `success_rate`, plus the devices' explicit `reported` outcomes |

**Cohorts & pins**

| endpoint | scope | what it does |
|---|---|---|
| `GET /api/v1/admin/cohorts` | observe | cohorts in use, device count each |
| `POST /api/v1/admin/cohorts/assign` | manage | move devices into a cohort — `device_ids` (surgical) or `product_id` (every device of the product), exactly one; devices not yours are skipped, the count says how many landed |
| `PATCH /api/v1/admin/devices/{id}/pin` | manage | pin/unpin one device (`release_id: null` unpins) |
| `POST /api/v1/admin/cohorts/pin` | manage | pin/unpin a whole (product, cohort) |

**Devices & the fleet**

| endpoint | scope | what it does |
|---|---|---|
| `GET /api/v1/admin/fleet` | observe | the summary `client fleet` prints: `total`, `by_version`, `by_fallback`, `fell_back`, `unconfirmed` |
| `GET /api/v1/admin/fleet/bases` | observe | the distinct (version, exact-bytes) bases the fleet is running, with device counts — what `build ota-romfs --delta-fleet` plans against |
| `GET /api/v1/admin/devices` | observe | per-device rows (paged; `product_id`/`cohort` filters) |
| `GET /api/v1/admin/devices/{id}` | observe | one device, same shape as a list row |
| `POST /api/v1/admin/devices/{id}/account` | manage | (re)bind the device to the caller's account |
| `POST /api/v1/admin/devices/{id}/viewer-grant` | observe | mint a short-lived single-device viewing credential for a dashboard user (503 when live viewing isn't configured) |

**Accounts & tokens** (the operator scope, `accounts`)

| endpoint | what it does |
|---|---|
| `POST /api/v1/admin/accounts` / `GET …/accounts` | create (returns the first admin token **once**) / list |
| `PATCH …/accounts/{id}` | rename (empty is 400, a taken name is 409) |
| `POST …/accounts/{id}/deactivate` / `…/activate` | revoke all tokens + disable / re-enable (old tokens stay revoked — issue fresh ones) |
| `POST …/accounts/{id}/tokens` / `GET …/accounts/{id}/tokens` | issue (secret once; unknown scopes are 400; a deactivated account is 409) / list metadata |
| `POST …/tokens/{hash}/revoke` / `…/tokens/{hash}/rotate` | revoke / replace-and-revoke (same name, scopes, account) |

**Audit**

| endpoint | scope | what it does |
|---|---|---|
| `GET /api/v1/admin/audit` | observe | the append-only log: every publish, rollout change (auto-pauses included), pin, assignment, bind, and account/token event, each with its actor |

## Paging

The collection reads (`/releases`, `/rollouts`, `/devices`) take `limit` (default 100)
and `offset`, and every response carries **`total`** beside its rows:

```json
{ "releases": [ ... ], "total": 412 }
```

`total` is what makes a page safe to consume — without it, a full page is
indistinguishable from a complete list. It is account-scoped like the rows it counts, so
it never discloses the size of another tenant's fleet.

`/audit` pages differently on purpose: it is an append-only log, so it takes `since`
(each event's sequence number is a cursor) rather than an offset — a cursor can't skip or
repeat entries when new ones arrive mid-page, which an offset can.

## The OpenAPI contract

Every operation documents its response schema, so `/docs` shows real shapes and
`/openapi.json` can drive a generated client. The schemas are attached as
**documentation, not enforcement**: responses are never filtered through them, so a field
the schema lags behind on still reaches the caller — the cost of a stale schema is an
incomplete doc, never lost data.

## See also

- [15 · The client](15-the-client.md) — the CLI over this API.
- [Threat model](../reference/threat-model.md) — why a compromised server still can't
  forge an update.

---

*[← 16 · The update server](16-update-server.md) · [Index](00-introduction.md)*

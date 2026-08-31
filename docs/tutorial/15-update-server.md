# The update server

*[← 14 · Recovery](14-recovery.md) · [Index](00-introduction.md) · [16 · The fleet →](16-fleet.md)*

---

`openmv-ota` can build and sign OTA images, deltas, and manifests, and a device can
`install(manifest_url)` — but nothing decides *which* URL a device fetches, hosts the artifacts,
or drives a fleet rollout. The **update server** is that missing piece: a central service that
hosts releases and stages them across a fleet. The **`client`** verb publishes to it, so you and CI never hand-write a URL.

Two deployment shapes run the **same software**:

- **OpenMV-hosted (the default):** OpenMV runs the server + website, so there is nothing to
  deploy — you publish releases with your account's token and everything below (database,
  bucket, registration credentials) is already supplied.
- **Self-hosted:** you run your own server — your own Render/Postgres/R2. The
  Dockerfile, `render.yaml`, and `docker-compose.yml` under
  [src/openmv_ota/server/deploy/](../../src/openmv_ota/server/deploy/) make it turnkey.

## Two things the server never does

**It never holds a signing key.** Releases are signed *locally* by `build ota-romfs` (the private
keys never leave your build host), and the device verifies the signed manifest against the keys
baked into its firmware. The server only stores and distributes already-signed bytes and runs
rollout *policy*. A fully compromised server cannot forge an update a device will accept — the
worst it can do is serve stale bytes or nothing. Because the manifest's artifact URLs are
**relative filenames**, the server serves the manifest untouched and co-locates the
`-ota.img.gz`/`-ota.delta.gz` beside it — no rewriting, no re-signing.

**It never serves an unregistered device.** Every deployment validates each camera against
OpenMV's central registration server. An unregistered `(board, id)` gets `{update: false}` and
**zero stored state** — no device row, no telemetry, no cache entry — so unknown ids can never
grow the database or storage. Registration is required: the two registration settings below
carry the verify endpoint and an OpenMV-issued token tied to your account.

## Configuration

Settings come from `OPENMV_OTA_*` environment variables (Render's bare `PORT` and `DATABASE_URL`
are also honored).

| setting | env var | notes |
| --- | --- | --- |
| base URL | `OPENMV_OTA_BASE_URL` | public https origin, for building capability URLs |
| port | `PORT` / `OPENMV_OTA_PORT` | bind port (default 8080) |
| storage backend | `OPENMV_OTA_STORAGE_BACKEND` | `local` (disk, dev) or `s3` (R2/S3, prod) |
| bucket + keys | `OPENMV_OTA_S3_BUCKET`, `OPENMV_OTA_S3_ENDPOINT_URL`, `OPENMV_OTA_S3_REGION`, `OPENMV_OTA_S3_ACCESS_KEY_ID`, `OPENMV_OTA_S3_SECRET_ACCESS_KEY` | R2/S3/MinIO |
| database | `DATABASE_URL` / `OPENMV_OTA_DATABASE_URL` | `postgresql://…` (prod) or `sqlite:///./ota.db` (dev) |
| registration | `OPENMV_OTA_SWD_IDS_VERIFY_URL`, `OPENMV_OTA_SWD_IDS_VERIFY_TOKEN` | **required** — the registration verify endpoint + its OpenMV-issued token |
| admin bootstrap | `OPENMV_OTA_ADMIN_BOOTSTRAP_TOKEN` | seeds the first admin token on `server init` |
| cohort salt | `OPENMV_OTA_COHORT_SALT` | the server HMAC secret; persisted if unset |
| rate + backoff | `OPENMV_OTA_CHECKIN_RATE_PER_MIN`, `OPENMV_OTA_POLL_AFTER_S`, `OPENMV_OTA_CAPABILITY_TTL` | tunables |
| browser UI origins | `OPENMV_OTA_CORS_ALLOW_ORIGINS` | comma-separated origins allowed to call this API **cross-origin**, e.g. `https://cloud.openmv.io`. Empty by default = no CORS headers at all. Needed only when a UI is served from a *different* origin than this app; a same-origin UI, or one that proxies through its own backend, leaves it unset. `*` is **refused at startup** -- name the origins |
| trusted proxy | `OPENMV_OTA_TRUSTED_PROXY_IPS` | which upstream peers may set `X-Forwarded-For`; set `*` behind a PaaS proxy (Render/Fly) so the per-IP rate limiter sees the real client, not the proxy |
| board codes | `OPENMV_OTA_BOARD_CODE_OVERRIDES` | JSON map to add/correct firmware-name → registration-code translations without a redeploy |
| unverified boards | `OPENMV_OTA_UNVERIFIED_BOARDS` | JSON list of firmware board names the registry never registers (Arduino boards, pre-registration M4); their registration check is bypassed and OTA is served read-only (no device row, so still zero-footprint). Defaults to those known board types; override to change the set |
| OpenMV Live | `OPENMV_LIVE_RELAY_URL`, `OPENMV_LIVE_TOKEN_SECRET`, `OPENMV_OTA_LIVE_TOKEN_TTL` | when the relay URL **and** secret are both set, every registered device's check-in response carries a `live` grant: a per-stream map of ready-made `camera_url` (WebSocket push) + `poll_url` (deep-sleep wake check) URLs sharing ONE HMAC device token (TTL default 24 h, renewed each check-in). The check-in's `streams` list names the device's image streams (multi-camera boards, virtual streams); names are sanitized and capped, defaulting to a single stream `"0"`. The secret is the live-relay worker's `OPENMV_LIVE_TOKEN_SECRET`. Unregistered and bypassed boards never get a grant |
| OpenMV datalake | `OPENMV_DATALAKE_URL` (reuses `OPENMV_LIVE_TOKEN_SECRET`) | when set with the shared secret, a registered device's check-in also carries an `ingest` grant: a ready-made ingest `url` (the device appends a topic, e.g. `console`) + an HMAC `ingest` token whose subject binds the **account** (`account/device`), so a device can't attribute data to another account. Same TTL/renewal as the Live grant; unregistered and bypassed boards never get one |

`openmv-ota server check` validates the resolved settings (secrets redacted) and reports what's
missing before you deploy.

## Running it

```
openmv-ota server check      # validate settings (deploy preflight)
openmv-ota server init       # migrate the schema + seed/print the admin token (idempotent)
openmv-ota server run        # start the ASGI app (uvicorn), binds $PORT / 0.0.0.0
openmv-ota server migrate    # apply pending metadata-store migrations
openmv-ota server token issue --name ci --scope publish   # mint a scoped token (shown once)
openmv-ota server token list | revoke <hash>
```

`server init` seeds one admin token: from `OPENMV_OTA_ADMIN_BOOTSTRAP_TOKEN` if set, otherwise a
fresh one printed **once** (only the hash is stored — it is not recoverable). Tokens carry scopes:
`publish` (publish releases), `manage` (all fleet changes — rollouts, cohorts, pins, device
binds), `observe` (read everything — fleet, releases, rollouts, devices, audit), and
the privileged operator scope `accounts` (create/list accounts — held by the bootstrap/root
token, not by a regular account's tokens).

## Accounts (multi-tenancy)

A product is namespaced by the maker's **account**: `(account_id, product_id)` is the real
identity, so a `product_id` only has to be unique *within* an account. Every admin credential
belongs to an account, and every admin read/write is scoped to it — one tenant can never see or
touch another's releases, rollouts, devices, or audit (cross-account by-id lookups return `404`,
so probing leaks nothing). The `account_id` is baked into the firmware (`[product].account_id` →
`system.json` → the check-in), so a device is only ever offered its own account's releases.

`''` is the **implicit single account**: a self-host that never creates an account keeps its
bootstrap token, publishes under `''`, and sees everything — unchanged. To run several tenants on
one server:

```
openmv-ota server account create --name "DroneCo"      # -> an account_id + its first admin token
openmv-ota server account list
openmv-ota server token issue --name ci --account <account_id>   # more tokens for an account
```

The same is available remotely to an `accounts` token: `POST /api/v1/admin/accounts`
(`client account create --name …`) returns the new `account_id` + its first token once, and
`GET /api/v1/admin/accounts` (`client account list`) lists them. An operator (re)binds a device to
an account with `POST /api/v1/admin/devices/{id}/account` (`client bind --id …`).

## Deploying (self-hosted only)

On the OpenMV-hosted service there is nothing to deploy. Self-hosting starts here: the base
`pip install openmv-ota` stays lean, and the server needs the extras:

```
pip install "openmv-ota[server]"                          # fastapi/uvicorn + local-disk + sqlite
pip install "openmv-ota[server,server-s3,server-postgres]"  # + R2/S3 + Postgres (prod)
```

The [deploy/](../../src/openmv_ota/server/deploy/) directory ships turnkey artifacts:

- **`Dockerfile`** — multi-stage build; the entrypoint runs `server init` (idempotent) then
  `server run`.
- **`render.yaml`** — a Render Blueprint: a stateless `web` service + a managed Postgres. Bring an
  R2/S3 bucket and your OTA-verify token; the cohort salt and admin token are generated once and
  kept by Render. `render blueprint launch`, then fill the `sync:false` secrets.
- **`fly.toml`** — the Fly.io equivalent (external Postgres + R2/S3).
- **`docker-compose.yml`** — a full local stack (server + Postgres + MinIO) for evaluation:
  `SWD_IDS_VERIFY_URL=… SWD_IDS_VERIFY_TOKEN=… docker compose up --build`.

The server is **stateless** — artifacts live in object storage, metadata in Postgres — so it scales
horizontally. Local-disk + SQLite is for dev and tests only.

---

*[← 14 · Recovery](14-recovery.md) · [Index](00-introduction.md) · [16 · The fleet →](16-fleet.md)*

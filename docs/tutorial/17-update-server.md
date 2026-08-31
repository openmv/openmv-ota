# The update server

*[← 16 · Operating the fleet](16-operating-the-fleet.md) · [Index](00-introduction.md) · [18 · The server API →](18-server-api.md)*

---

The update server is the central service the `client` verb drives: it hosts the releases
you publish, decides which camera is offered what, and records what the fleet did about
it. Two deployment shapes run the **same software**:

- **OpenMV-hosted (the default):** OpenMV runs the server + website, so there is nothing
  to deploy — you publish releases with your account's token and everything on this page
  is already configured.
- **Self-hosted:** you run your own server — your own PaaS/Postgres/object storage. The
  Dockerfile, `render.yaml`, `fly.toml`, and `docker-compose.yml` under
  [src/openmv_ota/server/deploy/](../../src/openmv_ota/server/deploy/) make it turnkey.

Either way, this page is what the thing actually *does* — worth reading even if you never
deploy one, because it is the other half of every `client` command.

## Two things the server never does

**It never holds a signing key.** Releases are signed *locally* by `build ota-romfs` (the
private keys never leave your build host), and the device verifies the signed manifest
against the keys baked into its firmware. The server only stores and distributes
already-signed bytes and runs rollout *policy*. A fully compromised server cannot forge
an update a device will accept — the worst it can do is serve stale bytes or nothing.
Because the manifest's artifact URLs are **relative filenames**, the server serves the
manifest untouched and co-locates the `-ota.img.gz`/`-ota.delta.gz` beside it — no
rewriting, no re-signing.

**It never serves an unregistered device.** Every deployment validates each camera
against OpenMV's central registration server, using what every check-in already carries:
the **board name** (`OPENMV_N6`, …) and the **device id** (the MCU's unique hardware id).
An unregistered pair gets `{update: false}` and **zero stored state** — no device row, no
telemetry, no cache entry — so unknown ids can never grow the database or storage.
Registration is required: the two registration settings below carry the verify endpoint
and an OpenMV-issued token tied to your account.

## What one check-in does

The heart of the server is `POST /api/v1/check` — what every camera calls on its poll
interval. In order:

1. **Rate limit** per client IP (429 with a retry hint when exceeded).
2. **Registration gate** — unregistered pairs are answered `{update: false}` and leave
   nothing behind. A short list of board types the registry structurally never registers
   (legacy Arduino boards, pre-registration M4s) bypasses the check and is served OTA
   **read-only**: offers work, but no device row is written, so a fake id still can't
   grow the database.
3. **Account binding** — the device's account is learned from its first valid check-in
   and **sticky** from then on (an admin `bind` overrides it). A later boot that reports
   a different or empty account — a factory-state fallback image, say — can't strand the
   device in the wrong tenant.
4. **The offer decision** — a device **pin** wins, then a **cohort pin**, then the active
   rollout for the device's cohort. Whatever the source, an offer only happens when it's
   an *upgrade* over what the device reports running, the device is *settled* (not
   mid-trial — its fallback slot is worth more than a new download), and — for a
   rollout — the device's stable hash falls inside the current percent.
5. **Rollout accounting** — the check-in feeds the rollout's counters: newly offered
   devices bump `attempted`, a device now running the offered release bumps `updated`,
   and a device transitioning into a fallback bumps `failures`. When the failure rate
   among offered devices crosses the rollout's threshold, the rollout **auto-pauses**
   and the audit log records it.
6. **The device row** — version, slot, confirmation state, fallback identity, cohort —
   is upserted; this is what `client fleet` and `client devices` summarize.
7. **The answer** — `{update: false, poll_after_s: …}` in the common case; on an offer,
   a short-lived download URL for the release's manifest. Where the deployment is wired
   for them, the answer also carries per-device **grants** for OpenMV's live-viewing and
   data-ingest services (below).

Downloads then go through the **capability gateway**: the offer's URL embeds an
unguessable, expiring token that authorizes the whole bundle — the manifest and every
image/delta beside it. Devices report their terminal outcome (`installed` / `failed`)
back explicitly, which is what the rollout status counts as `reported`.

## Running your own

The base `pip install openmv-ota` stays lean; the server needs extras:

```
pip install "openmv-ota[server]"                            # fastapi/uvicorn + local disk + sqlite
pip install "openmv-ota[server,server-s3,server-postgres]"  # + R2/S3 + Postgres (prod)
```

The lifecycle is four verbs:

```
openmv-ota server check      # validate the resolved settings (deploy preflight)
openmv-ota server init       # migrate the schema + one-time bootstrap (idempotent)
openmv-ota server run        # start the ASGI app (uvicorn), binds $PORT / 0.0.0.0
openmv-ota server migrate    # apply pending schema migrations (upgrades)
```

- **`check`** prints every resolved setting as `key = value` lines (secrets redacted to
  `***`) and lists anything required that's missing — run it before every deploy.
- **`init`** migrates the database, persists the server's HMAC secret (generated if you
  didn't set one), and seeds the first **admin token**: from
  `OPENMV_OTA_ADMIN_BOOTSTRAP_TOKEN` if set, otherwise freshly generated and printed
  **once** — only its hash is stored, so it is not recoverable. It's idempotent, which is
  why the container entrypoint just runs `init` then `run`.
- **`run`** also migrates + seeds on the way up, so a plain `server run` on a fresh
  database works.

The server is **stateless** — artifacts live in object storage, metadata in Postgres — so
it scales horizontally. Local disk + SQLite is for dev and evaluation only.

## Configuration, feature by feature

Settings come from `OPENMV_OTA_*` environment variables (the bare `PORT` and
`DATABASE_URL` that PaaS platforms inject are also honored). Grouped by the feature each
serves:

**Identity & HTTP**

| env var | what it does |
|---|---|
| `OPENMV_OTA_BASE_URL` | the public https origin — used to build the download URLs handed to devices |
| `PORT` / `OPENMV_OTA_PORT` | bind port (default 8080) |

**Storage (the artifact bytes)**

| env var | what it does |
|---|---|
| `OPENMV_OTA_STORAGE_BACKEND` | `local` (disk, dev) or `s3` (R2/S3/MinIO, prod) |
| `OPENMV_OTA_STORAGE_LOCATION` | the local backend's directory (default `./ota-storage`) |
| `OPENMV_OTA_S3_BUCKET`, `…_S3_ENDPOINT_URL`, `…_S3_REGION`, `…_S3_ACCESS_KEY_ID`, `…_S3_SECRET_ACCESS_KEY` | the s3 backend's bucket + credentials. With s3, artifact downloads 302-redirect to presigned URLs, so bandwidth offloads to object storage |

**Database (the metadata)**

| env var | what it does |
|---|---|
| `DATABASE_URL` / `OPENMV_OTA_DATABASE_URL` | `postgresql://…` (prod) or `sqlite:///./ota.db` (dev). Holds devices, releases, rollouts, accounts, tokens, audit |

**Registration (required)**

| env var | what it does |
|---|---|
| `OPENMV_OTA_SWD_IDS_VERIFY_URL`, `OPENMV_OTA_SWD_IDS_VERIFY_TOKEN` | the registration verify endpoint + its OpenMV-issued token |
| `OPENMV_OTA_BOARD_CODE_OVERRIDES` | JSON map to add/correct firmware-name → registration-code translations without a redeploy |
| `OPENMV_OTA_UNVERIFIED_BOARDS` | JSON list of board names the registry never registers; served read-only as described above. Defaults to the known set — override to change it |

**Device check-in behavior**

| env var | what it does |
|---|---|
| `OPENMV_OTA_CHECKIN_RATE_PER_MIN` | per-IP device rate limit (default 60; 0 disables) |
| `OPENMV_OTA_POLL_AFTER_S` | the backoff devices are told before polling again (default 3600) |
| `OPENMV_OTA_CAPABILITY_TTL` | lifetime of a download token (default 3600 s) |
| `OPENMV_OTA_COHORT_SALT` | the server's HMAC secret (download tokens, stable staging). Persisted at `init` if unset; must be shared for tokens to verify across workers |

**Admin auth**

| env var | what it does |
|---|---|
| `OPENMV_OTA_ADMIN_BOOTSTRAP_TOKEN` | seeds the first admin token at `server init` (else one is generated and printed once) |

**Browser dashboards (only when a UI calls the API cross-origin)**

| env var | what it does |
|---|---|
| `OPENMV_OTA_CORS_ALLOW_ORIGINS` | comma-separated origins allowed to call the API from a browser, e.g. `https://dashboard.example.com`. Empty (the default) sends no CORS headers; `*` is **refused at startup** — a wildcard would let any page on the internet read admin responses with a token it obtained some other way |
| `OPENMV_OTA_TRUSTED_PROXY_IPS` | which upstream peers may set `X-Forwarded-For`; set `*` behind a PaaS proxy so the per-IP rate limiter sees the real client, not the proxy |

**OpenMV service grants (optional integrations)**

| env var | what it does |
|---|---|
| `OPENMV_LIVE_RELAY_URL` + `OPENMV_LIVE_TOKEN_SECRET` | when both are set, every registered device's check-in answer carries a `live` grant: ready-made per-stream URLs (WebSocket push + a deep-sleep wake poll) under one expiring device token, renewed each check-in |
| `OPENMV_DATALAKE_URL` (reuses the same secret) | adds an `ingest` grant the same way: an ingest URL + token whose subject binds the *account*, so a device can't attribute data to another tenant |
| `OPENMV_OTA_LIVE_TOKEN_TTL` | grant lifetime (default 24 h) |

Unregistered and read-only-bypassed boards never receive a grant.

One more setting exists — `OPENMV_OTA_TEST_OFFER_DOWNGRADES` — and it is **test-only**:
it makes the server willing to *offer* a release at or below a device's current version,
which a correct server never does, and which is the only way to exercise the device's own
anti-rollback rejection on real hardware. Devices still refuse the downgrade themselves.
`server check` flags it and the app logs a loud warning when it's on; never set it in
production.

## Accounts and tokens

A product is namespaced by the maker's **account**: `(account_id, product_id)` is the
real identity, so a `product_id` only has to be unique within an account. Every admin
credential belongs to an account, every read and write is scoped to it, and cross-account
lookups return not-found — probing leaks nothing. The `account_id` is baked into the
firmware (`[product].account_id` → `system.json` → the check-in), so a device is only
ever offered its own account's releases.

`''` is the **implicit single account**: a self-host that never creates an account keeps
its bootstrap token, publishes under `''`, and sees everything. To run several tenants on
one server, the same account/token management the client offers remotely also exists as
local server verbs (no API round-trip, direct database access):

```
openmv-ota server account create --name "DroneCo"    # an account_id + its first admin token
openmv-ota server account list | rename | deactivate | activate
openmv-ota server token issue --name ci --scope publish --account-id acct_7bd21c50e83a94f1
openmv-ota server token list | revoke <hash> | rotate <hash>
```

Tokens carry scopes — `publish`, `manage`, `observe` per account, plus the privileged
operator scope `accounts` (create/list accounts; held by the bootstrap token, never by a
regular account's tokens). Secrets print once; the store keeps hashes.

## Deploying (self-hosted only)

The [deploy/](../../src/openmv_ota/server/deploy/) directory ships turnkey artifacts:

- **`Dockerfile`** — multi-stage build; the entrypoint runs `server init` (idempotent)
  then `server run`.
- **`render.yaml`** — a Render Blueprint: a stateless `web` service + managed Postgres.
  Bring an R2/S3 bucket and your registration token; the HMAC secret and admin token are
  generated once and kept by the platform. `render blueprint launch`, then fill the
  `sync:false` secrets.
- **`fly.toml`** — the Fly.io equivalent (external Postgres + R2/S3).
- **`docker-compose.yml`** — a full local stack (server + Postgres + MinIO) for
  evaluation: `SWD_IDS_VERIFY_URL=… SWD_IDS_VERIFY_TOKEN=… docker compose up --build`.

## See also

- [18 · The server API](18-server-api.md) — every endpoint, for scripts and dashboards.
- [Threat model](../reference/threat-model.md) — the trust root and why the server never
  holds a key.

---

*[← 16 · Operating the fleet](16-operating-the-fleet.md) · [Index](00-introduction.md) · [18 · The server API →](18-server-api.md)*

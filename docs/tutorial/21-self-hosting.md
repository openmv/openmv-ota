# Self-hosting

*[← 20 · The update server](20-update-server.md) · [Index](00-introduction.md) · [22 · The device API →](22-device-api.md)*

---

On the OpenMV-hosted service there is nothing to deploy. This page is the other route:
running the same server yourself — installing it, configuring each feature, and the
turnkey deploy artifacts.

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

**Registration (recommended)**

| env var | what it does |
|---|---|
| `OPENMV_OTA_SWD_IDS_VERIFY_URL`, `OPENMV_OTA_SWD_IDS_VERIFY_TOKEN` | the registration verify endpoint + its OpenMV-issued token. **Leave both unset and the server still works, read-only**: updates are served to every device, but nothing is logged — no device registry, telemetry, or grants (`server run` says so at startup). Attach registration to get the fleet-tracking half of the product |

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
| `OPENMV_OTA_LIVE_TOKEN_TTL` | Live grant lifetime (default 24 h) |
| `OPENMV_DATALAKE_URL` + `OPENMV_DATALAKE_TOKEN_SECRET` | when both are set, check-ins also carry an `ingest` grant: an ingest URL + token whose subject binds the *account*, so a device can't attribute data to another tenant. Deliberately its **own** secret — the two integrations rotate and fail independently |
| `OPENMV_OTA_DATALAKE_TOKEN_TTL` | ingest grant lifetime (default 24 h) |

Unregistered and read-only-bypassed boards never receive a grant.

One more setting exists — `OPENMV_OTA_TEST_OFFER_DOWNGRADES` — and it is **test-only**:
it makes the server willing to *offer* a release at or below a device's current version,
which a correct server never does, and which is the only way to exercise the device's own
anti-rollback rejection on real hardware. Devices still refuse the downgrade themselves.
`server check` flags it and the app logs a loud warning when it's on; never set it in
production.

## Accounts on a self-host

The account model — the implicit `''` single account, the bootstrap token, scopes, and
why only the operator can manage accounts — is [page 19](19-accounts-and-tokens.md).
What a self-host adds is the **local** spelling of the same operations: the `server`
verbs act directly on the database, no API round-trip, so they work before the server
is even running:

```
openmv-ota server account create --name "DroneCo"    # an account_id + its first working token
openmv-ota server account list | rename | deactivate | activate
openmv-ota server token issue --name ci --scope publish --account-id acct_7bd21c50e83a94f1
openmv-ota server token list | revoke <hash> | rotate <hash>
```

## Deploying (self-hosted only)

The [deploy/](../../src/openmv_ota/server/deploy/) directory ships turnkey artifacts:

- **`Dockerfile`** — multi-stage build; the entrypoint runs `server init` (idempotent)
  then `server run`. This is the whole deployment story: any platform that runs a
  container image can host the server — bring a Postgres, an S3-compatible bucket, and
  the settings above.
- **`docker-compose.yml`** — a full local stack (server + Postgres + MinIO) for
  evaluation: `SWD_IDS_VERIFY_URL=… SWD_IDS_VERIFY_TOKEN=… docker compose up --build`.

---

*[← 20 · The update server](20-update-server.md) · [Index](00-introduction.md) · [22 · The device API →](22-device-api.md)*

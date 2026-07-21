# The update server

`openmv-ota` can build and sign OTA images, deltas, and manifests, and a device can
`install(manifest_url)` — but nothing decides *which* URL a device fetches, hosts the artifacts,
or drives a fleet rollout. The **update server** is that missing piece: a central service that
hosts releases and stages them across a fleet. The **`client`** verb publishes to it, so you (and
CI) never hand-write a URL.

Two deployment shapes run the **same software**:

- **Self-hosted (the default):** you run your own server — your own Render/Postgres/R2. The
  Dockerfile, `render.yaml`, and `docker-compose.yml` under
  [src/openmv_ota/server/deploy/](../src/openmv_ota/server/deploy/) make it turnkey.
- **OpenMV-hosted:** OpenMV runs a server + website so you don't have to. That website embeds this
  package via `create_app()` and supplies the database, bucket, and registration credentials.

## Two things the server never does

**It never holds a signing key.** Releases are signed *locally* by `build ota-romfs` (the private
keys never leave your build host), and the device verifies the signed manifest against the keys
baked into its firmware. The server only stores and distributes already-signed bytes and runs
rollout *policy*. A fully compromised server cannot forge an update a device will accept — the
worst it can do is serve stale bytes or nothing. Because the manifest's artifact URLs are
**relative filenames**, the server serves the manifest untouched and co-locates the
`-ota.img.gz`/`-ota.delta.gz` beside it — no rewriting, no re-signing.

**It never serves an unregistered device.** Every deployment queries OpenMV's central registration
registry (openmv-swd-ids) to validate each camera. An unregistered `(board, id)` gets
`{update: false}` and **zero stored state** — no device row, no telemetry, no artifact, no cache
entry. This is a storage-exhaustion defense: the device id is attacker-controlled, so anything
allocated per-id turns cost into `O(attacker requests)`. The gate caps allocation to the bounded
registered fleet. Registration is required and configured with `SWD_IDS_VERIFY_URL` +
`SWD_IDS_VERIFY_TOKEN` (an OpenMV-issued token tied to your account).

## Configuration

Settings come from `OPENMV_OTA_*` environment variables (Render's bare `PORT` and `DATABASE_URL`
are also honored), or are injected programmatically via `create_app(ServerSettings(**overrides))`.

| setting | env var | notes |
| --- | --- | --- |
| base URL | `OPENMV_OTA_BASE_URL` | public https origin, for building capability URLs |
| port | `PORT` / `OPENMV_OTA_PORT` | bind port (default 8080) |
| storage backend | `OPENMV_OTA_STORAGE_BACKEND` | `local` (disk, dev) or `s3` (R2/S3, prod) |
| bucket + keys | `OPENMV_OTA_S3_BUCKET`, `OPENMV_OTA_S3_ENDPOINT_URL`, `OPENMV_OTA_S3_REGION`, `OPENMV_OTA_S3_ACCESS_KEY_ID`, `OPENMV_OTA_S3_SECRET_ACCESS_KEY` | R2/S3/MinIO |
| database | `DATABASE_URL` / `OPENMV_OTA_DATABASE_URL` | `postgresql://…` (prod) or `sqlite:///./ota.db` (dev) |
| registration | `OPENMV_OTA_SWD_IDS_VERIFY_URL`, `OPENMV_OTA_SWD_IDS_VERIFY_TOKEN` | **required** — the swd-ids verify endpoint + token |
| admin bootstrap | `OPENMV_OTA_ADMIN_BOOTSTRAP_TOKEN` | seeds the first admin token on `server init` |
| cohort salt | `OPENMV_OTA_COHORT_SALT` | the server HMAC secret; persisted if unset |
| rate + backoff | `OPENMV_OTA_CHECKIN_RATE_PER_MIN`, `OPENMV_OTA_POLL_AFTER_S`, `OPENMV_OTA_CAPABILITY_TTL` | tunables |
| trusted proxy | `OPENMV_OTA_TRUSTED_PROXY_IPS` | which upstream peers may set `X-Forwarded-For`; set `*` behind a PaaS proxy (Render/Fly) so the per-IP rate limiter sees the real client, not the proxy |
| board codes | `OPENMV_OTA_BOARD_CODE_OVERRIDES` | JSON map to add/correct firmware-name → swd-ids-code translations without a redeploy |
| unverified boards | `OPENMV_OTA_UNVERIFIED_BOARDS` | JSON list of firmware board names swd-ids never registers (Arduino boards, pre-registration M4); their registration check is bypassed and OTA is served read-only (no device row, so still zero-footprint). Defaults to those known board types; override to change the set |
| OpenMV Live | `OPENMV_LIVE_RELAY_URL`, `OPENMV_LIVE_TOKEN_SECRET`, `OPENMV_OTA_LIVE_TOKEN_TTL` | when the relay URL **and** secret are both set, every registered device's check-in response carries a `live` grant: ready-made `camera_url` (WebSocket stream) + `poll_url` (deep-sleep wake check) with an HMAC camera token (TTL default 24 h, renewed each check-in). The secret is the live-relay worker's `OPENMV_LIVE_TOKEN_SECRET`. Unregistered and bypassed boards never get a grant |

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
`publish` (publish), `manage` (promote/pause/rollback), `observe` (observe), and
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

**Hosted identity seam.** `create_app(admin_auth=…)` lets OpenMV's website inject its own auth
object (`authenticate(header) -> Principal`) that resolves a logged-in maker to their
`account_id`; the scoping then follows that Principal, with no `admin_tokens` rows involved. The
server holds the account→ownership mapping but never any billing or identity — that lives in the
website.

The base `pip install openmv-ota` stays lean; the server needs the extras:

```
pip install "openmv-ota[server]"                          # fastapi/uvicorn + local-disk + sqlite
pip install "openmv-ota[server,server-s3,server-postgres]"  # + R2/S3 + Postgres (prod)
```

## Deploying

The [deploy/](../src/openmv_ota/server/deploy/) directory ships turnkey artifacts:

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

## Publishing and rolling out (the `client`)

The `client` verb turns a `build ota-romfs` output into an upload + rollout without ever typing a
URL. Credentials resolve **flag > env (`OPENMV_OTA_SERVER`/`OPENMV_OTA_TOKEN`) > saved profile**
(`~/.config/openmv-ota/client.toml`), so CI runs stateless and humans `client login` once.

```
openmv-ota client login --server https://ota.example.com --token <admin-token>
openmv-ota client publish ./my-product -b OPENMV_N6 --rollout beta:5   # publish + stage to 5%
openmv-ota client rollout raise  --id <rollout-id> --percent 50        # promote as confidence grows
openmv-ota client rollout pause  --id <rollout-id>                     # halt (auto-pauses on failures too)
openmv-ota client rollout resume --id <rollout-id>
openmv-ota client rollout rollback --id <rollout-id>                   # stop offering (shipped devices keep it)
openmv-ota client fleet | client devices [--board-id N] | client audit
```

`publish` uploads the exact signed bytes the build produced (`<board>-manifest.bin`,
`<board>-ota.img.gz`, and `<board>-ota.delta.gz` if present). The server derives all metadata from
the signed manifest — never from client-supplied JSON — verifies the artifacts against it, and
enforces anti-rollback (a lower `payload_version` is refused unless `--allow-republish`). A release
is **inert** until a rollout activates it.

CI happy path: `openmv-ota client publish --project . --rollout beta:5`.

## How a rollout is staged

A rollout offers a release to a growing slice of a cohort. Membership is **stable** across a
device's repeated polls and only *grows* as you raise the percentage — no per-request randomness, no
stored per-device flag:

```
bucket = sha256(rollout_id + ":" + device_id)[:4] % 10000
staged = bucket < percent * 100
```

Salting by `rollout_id` means a given device isn't always the canary. A device is never offered a
release at or below what it already runs (anti-rollback, which the device re-checks anyway). If the
fallback rate among offered devices crosses the failure threshold (~5%), the rollout **auto-pauses**
and records an audit event. Success is inferred from the next check-in (a new `payload_version`,
`confirmed`, no `fallback_reason`).

Artifacts are served through a **capability gateway**: an update response hands the device a
`/d/<token>/manifest.bin` URL whose unguessable token guards the whole bundle (the manifest's
relative siblings resolve under the same prefix); each GET 302-redirects to a short-lived
S3/R2-presigned URL, so bandwidth offloads to object storage. Tokens are only ever issued to
registered devices.

## See also

- [Building OTA images](romfs.md) — what `build ota-romfs` produces and how it's signed.
- [Threat model](threat-model.md) — the trust root and why the server never holds a key.

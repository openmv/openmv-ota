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
| browser UI origins | `OPENMV_OTA_CORS_ALLOW_ORIGINS` | comma-separated origins allowed to call this API **cross-origin**, e.g. `https://cloud.openmv.io`. Empty by default = no CORS headers at all. Needed only when a UI is served from a *different* origin than this app; a same-origin UI, or one that proxies through its own backend, leaves it unset. `*` is **refused at startup** -- name the origins |
| trusted proxy | `OPENMV_OTA_TRUSTED_PROXY_IPS` | which upstream peers may set `X-Forwarded-For`; set `*` behind a PaaS proxy (Render/Fly) so the per-IP rate limiter sees the real client, not the proxy |
| board codes | `OPENMV_OTA_BOARD_CODE_OVERRIDES` | JSON map to add/correct firmware-name → swd-ids-code translations without a redeploy |
| unverified boards | `OPENMV_OTA_UNVERIFIED_BOARDS` | JSON list of firmware board names swd-ids never registers (Arduino boards, pre-registration M4); their registration check is bypassed and OTA is served read-only (no device row, so still zero-footprint). Defaults to those known board types; override to change the set |
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
openmv-ota client fleet | client devices [--product-id N] | client audit
```

### The OpenAPI contract

Every operation documents its 200 — 31 typed JSON schemas plus the two binary downloads as
`application/gzip` — so `/docs` shows response shapes and a client can be generated from
`/openapi.json` instead of hand-written against guesses.

The schemas are attached as **documentation, not enforcement** (`responses={200: ...}`, never
`response_model`), and that distinction is deliberate. `response_model` *filters*: FastAPI drops
any field the model does not declare. Rows here come from `SELECT *`, so a schema one migration
behind would make a real column **silently vanish from the API** — measured, not hypothetical:
`Device` declares 17 fields while the store delivers 19, and one of the two undeclared ones
(`streams`) is what the viewer-grant endpoint reads. Documentation-only means a schema this
repo forgets to update costs an incomplete doc, never lost data.

Tightening to enforced `response_model` is a deliberate follow-up, once the shapes have settled
and a test proves model-vs-store parity.

### Paging the collection reads

`GET /releases`, `/rollouts` and `/devices` take `limit` (default **100**) and `offset`, and
each response carries **`total`** beside its rows:

```json
{ "releases": [ ... ], "total": 412 }
```

`total` is what makes the page safe to consume. Without it a full page is indistinguishable
from a truncated list — 100 rows back from a fleet of 412 looks exactly like a fleet of 100 —
and a UI cannot render "page 2 of N" at all. It is account-scoped like the rows it counts, so
it never discloses the size of another tenant's fleet.

(`/releases` and `/rollouts` previously defaulted to *no* limit and returned every row; the
bound is now the same number everywhere, so a caller need not remember which collection
happened to be unbounded.)

`/audit` pages differently on purpose: it is an append-only log, so it takes `since` (a
sequence cursor) rather than an offset — a cursor cannot skip or repeat entries when new ones
arrive mid-page, which an offset can.

`client fleet` is the rollout dashboard, and under A/B it reports **exposure** rather than
which slot a device happens to be running from:

| Field | What it answers |
|---|---|
| `by_version` | what the fleet is running |
| `by_fallback` | what it would fall back **to**. A fleet whose devices all have the previous release behind them is in a very different position from one where half report `unknown` — and that is invisible in `by_version` |
| `fell_back` | devices whose last boot rejected a slot. The direct rollout alarm |
| `unconfirmed` | devices running an image that has not confirmed itself yet. They are mid-trial, and therefore also **deferring** further updates until they settle |

### Delta bases, and why the server keeps every image

A device patches against **the release it is running**, so a fleet mid-rollout is spread over
several versions and one delta reaches only the devices still on its base. A release therefore
ships **one delta per base version still in the field**.

The server cannot build those for you, and that is structural rather than a gap: a delta has to
be named in the **signed** manifest, and the server never holds signing keys. So the maker
builds deltas locally — and needs the older *images* to diff against. The server keeps every
published image so a build machine does not have to:

```
openmv-ota client bases -b OPENMV_N6 --last 3 -o build/bases   # pull recent images back
openmv-ota build ota-romfs . --delta-from build/bases          # one delta per base
openmv-ota client publish . -b OPENMV_N6                       # uploads all of them
```

Lose the build directory, re-clone the repo, or hand the release to a colleague, and the bases
are still there.

Retention has **no depth limit** — images are small, and only you know how long a version stays
in the field, so nothing expires on its own. Reclaiming space is a deliberate act:

```
openmv-ota client prune --release rel_abc123      # delete that release's stored objects
```

The release **row** survives: it is the audit trail and the anti-rollback history, and it is
what lets `GET /releases/{id}/image` answer *"image is no longer retained"* rather than a bare
404 — a caller must be able to tell "existed, bytes gone" from "never existed". Pruning is
**refused while a rollout still offers that release**, because those are the devices
downloading it right now; pause or roll back first, or pass `--force` if you mean it. A release whose image has aged out of retention returns `410`-shaped
`404 image is no longer retained` — the release row survives its bytes, and that is a different
problem for the caller than "no such release".

`unknown` in `by_fallback` means the device did not say — a single-image board, which has no
fallback by design. `client devices` carries the same fallback per device, decoded
(`fallback_version`) alongside the packed `fallback_payload_version`.

`publish` uploads the exact signed bytes the build produced (`<board>-manifest.bin`,
`<board>-ota.img.gz`, and `<board>-ota.delta.gz` if present). The server derives all metadata from
the signed manifest — never from client-supplied JSON — verifies the artifacts against it, and
enforces anti-rollback (a lower `payload_version` is refused unless `--allow-republish`). A release
is **inert** until a rollout activates it.

CI happy path: `openmv-ota client publish --project . --rollout beta:5`.

## Every `client` command

The full remote surface, so nothing has to be discovered by guessing at `--help`. Read verbs
print JSON on stdout; write verbs print a one-line summary. All take `--server` / `--token`, or
use the profile saved by `login`.

| Command | What it does |
|---|---|
| `client login --server URL --token T` | save the server URL + admin token (also reads the token from stdin or `OPENMV_OTA_TOKEN`) |
| `client logout` | remove the saved profile |
| `client publish DIR -b BOARD [--rollout c:N]` | upload a built release, optionally staging it |
| `client bases DIR -b BOARD` | download recent release images to build deltas against |
| `client prune --release ID [--force]` | delete a release's stored artifacts, keeping its history row |
| `client rollout raise\|pause\|resume\|rollback --id ID` | drive a rollout (`raise` takes `--percent`) |
| `client cohort list` / `client cohort assign --cohort C --device ID` | see cohorts / put devices in one |
| `client pin device --id ID (--release R \| --clear)` | pin one device to a release, overriding rollouts |
| `client pin cohort --product-id N --cohort C --release R` | pin a whole cohort |
| `client bind --id ID` | (re)bind a device to the caller's account |
| `client fleet` / `client devices` / `client releases` / `client audit` | the read side (JSON) |
| `client account create\|list\|rename\|deactivate\|activate` | tenant accounts (needs the `accounts` scope) |
| `client token issue\|list\|revoke\|rotate` | an account's API tokens (`issue`/`rotate` return the secret **once**) |

Every verb takes **`--json`**, which prints the server's response verbatim instead of the
summary line — so publishing, issuing a token or assigning a cohort can be scripted without
parsing English:

```bash
rel=$(openmv-ota client publish ./p -b OPENMV_N6 --json | jq -r .release_id)
tok=$(openmv-ota client token issue --account "$acct" --name ci --json | jq -r .token)
```

Verbatim matters for the one-time secrets: `account create`, `token issue` and `token rotate`
return a token that exists for exactly that one response, and a script that cannot capture it
has to mint another. `publish --rollout` is two API calls, so its JSON nests the rollout under
`rollout` and leaves the release fields where a plain `publish` puts them.

`--scope` on `client token issue` accepts the same set the API validates against
(`publish`, `manage`, `observe`, `accounts`), so a typo fails at the prompt rather than as a
server 400. The default is the worker set: `publish, manage, observe`.

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

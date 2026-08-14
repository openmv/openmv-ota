# The fleet

*[← 10 · The update server](10-update-server.md) · [Index](00-introduction.md)*

---

The server hosts releases; the **`client`** verb is how you and CI drive it — publish a
build, stage a rollout, watch the fleet. Everything here talks to the admin HTTP API,
so anything the CLI does, other software (OpenMV's cloud, your scripts) can do too.

## Publishing and rolling out

Credentials resolve **flag > env (`OPENMV_OTA_SERVER`/`OPENMV_OTA_TOKEN`) > saved profile**
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

`publish` uploads the exact signed bytes the build produced (`<board>-manifest.bin`,
`<board>-ota.img.gz`, and `<board>-ota.delta.gz` if present). The server derives all metadata from
the signed manifest — never from client-supplied JSON — verifies the artifacts against it, and
enforces anti-rollback (a lower `payload_version` is refused unless `--allow-republish`). A release
is **inert** until a rollout activates it.

CI happy path: `openmv-ota client publish . -b OPENMV_N6 --rollout beta:5` — publish and
stage in one command.

## The OpenAPI contract

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

## Paging the collection reads

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

## Watching the fleet

`client fleet` is the rollout dashboard, and under A/B it reports **exposure** rather than
which slot a device happens to be running from:

| Field | What it answers |
|---|---|
| `by_version` | what the fleet is running |
| `by_fallback` | what it would fall back **to**. A fleet whose devices all have the previous release behind them is in a very different position from one where half report `unknown` — and that is invisible in `by_version` |
| `fell_back` | devices whose last boot rejected a slot. The direct rollout alarm |
| `unconfirmed` | devices running an image that has not confirmed itself yet. They are mid-trial, and therefore also **deferring** further updates until they settle |

`unknown` in `by_fallback` means the device did not say — a single-image board, which has no
fallback by design. `client devices` carries the same fallback per device, decoded
(`fallback_version`) alongside the packed `fallback_payload_version`.

## Delta bases, and why the server keeps every image

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
downloading it right now; pause or roll back first, or pass `--force` if you mean it.

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

- [Building OTA images](06-building.md) — what `build ota-romfs` produces and how it's signed.
- [Threat model](../reference/threat-model.md) — the trust root and why the server never holds a key.

---

*[← 10 · The update server](10-update-server.md) · [Index](00-introduction.md)*

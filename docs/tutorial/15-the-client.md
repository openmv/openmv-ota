# The client

*[← 14 · Recovery](14-recovery.md) · [Index](00-introduction.md) · [16 · The update server →](16-update-server.md)*

---

`openmv-ota build ota-romfs` leaves a signed release in `build/`; nothing so far decides
which camera downloads it. That is the **update server**'s job — a central service that
hosts your releases and stages them across the fleet — and **`openmv-ota client`** is how
you and CI drive it. Everything the client does goes through the server's admin HTTP API,
so anything it can do, your own scripts and dashboards can do too.

This page is the day-to-day workflow: log in once, publish a release, stage it out,
watch the fleet take it. You need two things — a **server URL** and an **admin token**.
On the OpenMV-hosted service (the default) both come with your account; a self-hosted
server issues its own.

## Logging in

`client login` saves your credentials so no later command needs them. The server URL
defaults to the OpenMV-hosted service, so out of the box only the token is needed:

```
$ openmv-ota client login --token <admin-token>
saved /home/you/.config/openmv-ota/client.toml
```

The token can also arrive on stdin or from `OPENMV_OTA_TOKEN`, so it never has to appear
in shell history. Every verb resolves its credentials the same way:

| source | when it wins |
|---|---|
| `--server` / `--token` on the verb | always (a one-off against another server) |
| `OPENMV_OTA_SERVER` / `OPENMV_OTA_TOKEN` | when no flag is given — how CI runs stateless |
| `~/.config/openmv-ota/client.toml` | what `login` wrote (mode 0600) |
| `https://ota.cloud.openmv.io` | the server URL when nothing above names one — the OpenMV-hosted service. There is no default token: a self-host points `--server`/env/`login` at its own deployment |

`client logout` deletes the file.

## Publishing a release

`client publish` uploads the exact signed bytes the build produced — the manifest, the
full image, and every delta the manifest declares:

```
$ openmv-ota client publish ./my-product -b OPENMV_N6
published rel_4f9c2a81d06b73ee  version 1.2.0  (full, ocdl)
```

The parenthetical lists the release's **representations** — the forms a device can
download it in: `full` is the whole image, `ocdl` is a delta patch (`ocdl` is the patch
format's name). Step by step:

1. It picks up `<board>-manifest.bin` and `<board>-ota.img.gz` from `build/` (or
   `-o DIR`). Every release has that one **full image**; it may also carry **deltas** —
   small patches, each against one specific older release (its *base*), so a device
   running that base downloads only the changes. The **signed manifest** names exactly
   which deltas belong to this release, and that is what publish reads — a declared delta
   missing from the directory is an error here, where the fix is local (`build ota-romfs`
   again), not a rejection from the server.
2. It also attaches the release's **SBOM** — the standard machine-readable list
   (CycloneDX) of every dependency and version built into this firmware, generated from
   the project's lock file. The server stores it beside the release, so "what exactly is
   in the release the fleet is running?" stays answerable later (CVE scans, compliance).
   If the SBOM can't be generated, publish proceeds anyway with a warning — it must never
   be the reason a release doesn't ship.
3. The server derives **all** release metadata (product, version, sizes, hashes, your
   account) from the signed manifest — never from anything the client asserts — and
   refuses an upload whose artifacts don't match it: an image whose sha256 or size
   disagrees, a declared delta that didn't arrive, an extra delta the manifest never
   named, a delta whose target size is wrong.
4. Publish-time anti-rollback: a `payload_version` at or below the newest already
   published for that product is refused. `--allow-republish` overrides it — the dev
   loop, where you rebuild the same version all afternoon.

A published release is **inert**: no device is offered it until a rollout (or a pin)
points at it.

## Cohorts

Before staging an update, decide who gets it first. **Cohorts** exist so a release
can go to less than everyone — a `beta` bench, a canary site, a customer. How they fit
the rest of your account, top to bottom:

- Your **account** holds everything — devices, releases, rollouts, cohorts, audit.
- **Each board you build for is its own product**: `project new` derives one product id
  per board and stamps it into `openmv-ota.toml` ([page 2](02-projects.md)); a release
  is built and published per board (`publish -b`) — which is what keeps an `OPENMV_N6`
  image from ever being offered to an `OPENMV_RT1060`.
- **Cohort names are free-form, account-wide labels on devices** — every device is in
  exactly one, starting in `__default__` until you move it, and devices of different
  products can share a name (`cohort list` counts a name across products unless you
  filter with `--product-id`).
- **Update targeting is always (product, cohort)**: a release fits one product, so a
  rollout reaches the `beta` devices *of its release's product*. Shipping one app
  version for a two-board product to `beta` is two publishes and two rollouts sharing
  the cohort name.

Use as many or as few names as you like — a fleet can live its whole life in
`__default__`; cohorts only exist to stage updates to a subset:

```
$ openmv-ota client cohort assign --cohort beta --device-id 30003d000851303436313832
assigned 1/1 device(s) to cohort beta

$ openmv-ota client cohort list
{
  "cohorts": [
    { "cohort": "__default__", "devices": 404 },
    { "cohort": "beta", "devices": 8 }
  ]
}

$ openmv-ota client cohort assign --cohort beta --product-id 396486252
assigned 412 device(s) (product 396486252) to cohort beta
```

`assign` takes exactly one selector: `--device-id` (repeatable) moves those exact
devices; `--product-id` moves every device of the product. `client devices` lists both
ids per device. Assignment is also removal — a device moved to `beta` leaves
`__default__` — and it counts only the devices that exist and are yours: the `1/1` in
the summary is what makes a typo'd id visible.

## Staging a rollout

The server never pushes anything: cameras poll, and each check-in is answered by
policy. A **rollout** is that policy's unit — an object of its own on the server,
separate from the release it carries. It binds together:

- **one release** — the thing to distribute;
- **one cohort** — the group of devices to distribute it to (`__default__` when you
  don't pass `--cohort`);
- **a percentage** — how much of that cohort is currently offered it;
- **a state** — `active`, `paused`, or `rolled_back`;
- **counters** — how many devices it was offered to (`attempted`), how many now run it
  (`updated`), how many fell back off it (`failures`).

When a device in the cohort checks in, it is offered the rollout's release only if all
three gates pass: the release is an **upgrade** over what the device reports running,
the device is **settled** (not mid-trial — a camera that hasn't confirmed its current
image is left alone, because the slot an install would overwrite is its only proven
fallback), and the device falls inside the current **percentage**.

Within one cohort, only one rollout offers at a time. Staging a new release to a cohort
mid-rollout — v1.3 while v1.2 is still going out — automatically pauses the v1.2
rollout (nothing is deleted: its counters stay readable, and the audit log records that
it was superseded). The cohort's check-ins are answered by the new rollout from then
on: devices that already took v1.2 are offered the upgrade, and devices the old rollout
never reached skip straight to v1.3.

Create one at publish time or stage an already-published release later — the same
`--cohort`/`--percent` flags either way, and the rollout's id comes back in the output:

```
$ openmv-ota client publish . -b OPENMV_N6 --cohort beta --percent 5
published rel_4f9c2a81d06b73ee  version 1.2.0  (full, ocdl)
rollout ro_1c3f88ba90d2e644  5.0%  cohort=beta

$ openmv-ota client rollout create --release-id rel_4f9c2a81d06b73ee --cohort beta --percent 5
rollout ro_1c3f88ba90d2e644  5.0%  cohort=beta
```

(`--percent` alone stages to `__default__`; on `publish`, `--percent` is what triggers
staging at all — without it the release is published and left inert.)

Which devices make up the staged percentage is a stable per-device hash, not a choice
you make or a list the server keeps:

```
bucket = sha256(rollout_id + ":" + device_id)[:4] % 10000
staged = bucket < percent * 100
```

A device's in/out verdict never flips while the percent holds, and raising the percent
only **adds** devices — the "raise it as confidence grows" model, with no per-request
randomness and no stored per-device flag. Salting by `rollout_id` means the same camera
isn't the canary in every rollout.

From there the lifecycle is four actions:

```
openmv-ota client rollout raise 50 --rollout-id ro_1c3f88ba90d2e644
openmv-ota client rollout pause  --rollout-id ro_1c3f88ba90d2e644
openmv-ota client rollout resume --rollout-id ro_1c3f88ba90d2e644
openmv-ota client rollout rollback --rollout-id ro_1c3f88ba90d2e644
```

| action | what happens |
|---|---|
| `raise N` | widen the staged slice to N percent. Percent is **monotonic** — lowering it is refused, because devices already offered the release can't be un-offered |
| `pause` | stop offering; resumable. The server also **auto-pauses** a rollout whose fallback rate among offered devices crosses its failure threshold (5% by default) and records an audit event |
| `resume` | start offering again |
| `rollback` | stop offering **permanently**. Devices that already took the release keep it — the server never downgrades a camera; the device's own anti-rollback wouldn't accept one anyway |

`client rollouts` lists them (so a lost id is always recoverable), and
`client rollout status --id` reads one rollout's score — the raise/pause decision in
four numbers:

```
$ openmv-ota client rollout status --rollout-id ro_1c3f88ba90d2e644
{
  "rollout_id": "ro_1c3f88ba90d2e644",
  "state": "active",
  "percent": 5.0,
  "attempted": 21,
  "updated": 19,
  "failures": 0,
  "success_rate": 0.9047619047619048,
  "reported": { "installed": 19, "failed": 0 }
}
```

## Watching the fleet

The read verbs print the server's JSON directly:

```
$ openmv-ota client fleet
{
  "total": 412,
  "by_version": { "1.2.0": 361, "1.1.0": 51 },
  "by_fallback": { "1.1.0": 358, "unknown": 54 },
  "fell_back": 2,
  "unconfirmed": 7
}
```

| field | what it answers |
|---|---|
| `by_version` | what the fleet is running |
| `by_fallback` | what it would fall back **to**. A fleet whose devices all have the previous release behind them is in a very different position from one where half report `unknown` — and that is invisible in `by_version` |
| `fell_back` | devices whose last boot rejected a slot — the direct rollout alarm |
| `unconfirmed` | devices mid-trial. They are also the devices deferring further updates until they settle |

`unknown` in `by_fallback` is a device that did not say — a single-image board, which has
no fallback by design.

- **`client devices`** — the per-device rows: version, slot, cohort, confirmation state,
  and the fallback decoded (`fallback_version`) beside the packed number the device
  reports. Filters: `--product-id`, `--cohort`; pages with `--limit`/`--offset`.
- **`client releases`** — the publish history, newest first.
- **`client audit`** — the append-only audit log: every publish, rollout change, pin,
  assignment, token event, and auto-pause, each with its actor. `--since SEQ` resumes
  from a sequence cursor, so a poller never skips or repeats entries.

## Pins

A pin overrides rollouts for one device or one whole cohort — "this camera runs exactly
this release":

```
openmv-ota client pin device --device-id 30003d000851303436313832 --release-id rel_4f9c2a81d06b73ee
openmv-ota client pin device --device-id 30003d000851303436313832 --clear
openmv-ota client pin cohort --product-id 396486252 --cohort beta --release-id rel_4f9c2a81d06b73ee
```

A device pin beats a cohort pin, and either beats the rollout. A pin only ever produces
an **offer** when it's an upgrade for a settled device; pinning to the version a camera
already runs (or older) simply holds it — no rollout reaches it, nothing downgrades.

`pin cohort` names the product because a cohort name is only meaningful per product —
the same name can exist under two products, so the pin binds the `(product, cohort)`
pair. A device pin doesn't need it: the device id alone is unique.

## Delta bases — and why the server keeps every image

A device patches against **the release it is running**, so a fleet mid-rollout is spread
over several versions and one delta reaches only the devices still on its base. A release
therefore ships one delta per base version still in the field — and the deltas must be
built **locally**, because a delta has to be named in the *signed* manifest and the
server never holds signing keys. What the maker needs are the older images to diff
against, and the server retains every published image precisely so a build machine
doesn't have to:

```
openmv-ota build ota-romfs . --delta-fleet                     # asks the server which bases
                                                               # the fleet actually runs, and
                                                               # builds one delta per base
# or by hand:
openmv-ota client bases -b OPENMV_N6 --last 3 -o build/bases   # pull recent images back
openmv-ota build ota-romfs . --delta-from build/bases          # one delta per base
openmv-ota client publish . -b OPENMV_N6                       # uploads all of them
```

`bases` writes `<board>-base-<version>.img.gz` files, exactly the naming
`--delta-from` picks up. Lose the build directory, re-clone the repo, or hand the release
to a colleague — the bases are still on the server.

Retention has **no depth limit**: images are small, and only you know how long a version
stays in the field. Reclaiming space is therefore a deliberate act:

```
openmv-ota client prune --release-id rel_4f9c2a81d06b73ee
```

The release **row** survives — it is the audit trail and the anti-rollback history — so
the server can answer "this release existed but its bytes are gone" rather than a bare
not-found. Pruning is refused while a rollout still offers that release (those are the
devices downloading it right now): pause or roll back first, or pass `--force` if you
mean it.

## Accounts, tokens, and binding

Releases, rollouts, devices, and the audit log are all namespaced by your **account** —
one tenant can never see or touch another's. Two verbs manage that layer; both need the
privileged `accounts` scope, which ordinary working tokens don't carry:

```
openmv-ota client account create --name "DroneCo"     # a new account + its first admin token
openmv-ota client account list | rename | deactivate | activate
openmv-ota client token issue --account-id acct_7bd21c50e83a94f1 --name ci --scope publish
openmv-ota client token list --account-id acct_7bd21c50e83a94f1
openmv-ota client token revoke  <token-hash>
openmv-ota client token rotate  <token-hash>          # replacement issued, old revoked
```

| about tokens | |
|---|---|
| scopes | `publish` (publish releases), `manage` (rollouts, cohorts, pins, binds), `observe` (read everything), `accounts` (the operator scope). `token issue` defaults to the worker set: publish, manage, observe |
| secrets | shown **once**, at issue/rotate — the server stores only a hash. `token list` shows metadata and hashes, never secrets |
| revocation | by hash. `deactivate` revokes every token an account has and blocks issuing new ones — admin access dies, but fielded devices keep being served, so a billing lapse never bricks a fleet |

`client bind --device-id DEVICE` (re)binds a device to **your** account — the recovery path when
a camera was first seen under the wrong account. A device's binding is otherwise learned
from its first valid check-in and sticky from then on.

## Scripting: `--json`

Every verb takes `--json`, which prints the server's response verbatim instead of the
summary line — so nothing has to be scripted by parsing English:

```bash
rel=$(openmv-ota client publish ./p -b OPENMV_N6 --json | jq -r .release_id)
tok=$(openmv-ota client token issue --account-id "$acct" --name ci --json | jq -r .token)
```

Verbatim matters most for the one-time secrets (`account create`, `token issue`,
`token rotate`): the token exists in exactly that one response, and a script that can't
capture it has to mint another. `publish --percent` is two API calls, so its JSON nests
the rollout under `rollout` and leaves the release fields where a plain `publish` puts
them — no special case for parsers.

## Every `client` command

| command | what it does |
|---|---|
| `client login --server URL [--token T]` | save the profile (token also via stdin / `OPENMV_OTA_TOKEN`) |
| `client logout` | remove the saved profile |
| `client publish DIR -b BOARD [--percent N [--cohort C]] [--allow-republish]` | upload a built release, optionally staging it |
| `client rollout create --release-id R --percent N [--cohort C]` | stage an already-published release |
| `client rollout raise\|pause\|resume\|rollback --rollout-id ID` | drive a rollout (`raise` takes the percent directly: `raise 50`) |
| `client rollout status --rollout-id ID` | one rollout's counters (JSON) |
| `client cohort list` / `cohort assign --cohort C (--device-id ID… \| --product-id N)` | see cohorts / move devices into one, surgically or by whole product |
| `client pin device --device-id ID (--release-id R \| --clear)` | pin one device, overriding rollouts |
| `client pin cohort --product-id N --cohort C (--release-id R \| --clear)` | pin a whole cohort |
| `client bases -b BOARD [--last N] [-o DIR]` | download recent images as delta bases |
| `client prune --release-id ID [--force]` | delete a release's stored artifacts, keep its history row |
| `client bind --device-id ID` | (re)bind a device to your account |
| `client fleet` / `devices` / `releases` / `rollouts` / `audit` | the read side (JSON) |
| `client account create\|list\|rename\|deactivate\|activate` | tenant accounts (needs `accounts`) |
| `client token issue\|list\|revoke\|rotate` | an account's API tokens (secrets shown once) |

## See also

- [16 · The update server](16-update-server.md) — what the service on the other end does,
  and how to run your own.
- [8 · Release artifacts](08-release-artifacts.md) — what `publish` uploads and how it's signed.

---

*[← 14 · Recovery](14-recovery.md) · [Index](00-introduction.md) · [16 · The update server →](16-update-server.md)*

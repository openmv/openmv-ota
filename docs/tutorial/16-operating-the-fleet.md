# Operating the fleet

*[← 15 · The client](15-the-client.md) · [Index](00-introduction.md) · [17 · The update server →](17-update-server.md)*

---

A release is out and a rollout is offering it. This page is the rest of the `client`
surface: watching the fleet take it, pinning exceptions, keeping delta bases, managing
accounts and tokens, and scripting all of it.

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
devices downloading it right now): pause or stop it first, or pass `--force` if you
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
| `client rollout raise\|pause\|resume\|stop --rollout-id ID` | drive a rollout (`raise` takes the percent directly: `raise 50`) |
| `client rollout status --rollout-id ID` | one rollout's counters (JSON) |
| `client cohort list` / `cohort assign --cohort C (--device-id ID… \| --product-id N)` | see cohorts / move devices into one, surgically or by whole product |
| `client cohort rename --cohort C --name N` / `cohort delete --cohort C` | relabel a cohort everywhere / retire it (devices return to `__default__`) |
| `client pin device --device-id ID (--release-id R \| --clear)` | pin one device, overriding rollouts |
| `client pin cohort --product-id N --cohort C (--release-id R \| --clear)` | pin a whole cohort |
| `client bases -b BOARD [--last N] [-o DIR]` | download recent images as delta bases |
| `client prune --release-id ID [--force]` | delete a release's stored artifacts, keep its history row |
| `client bind --device-id ID` | (re)bind a device to your account |
| `client fleet` / `devices` / `releases` / `rollouts` / `audit` | the read side (JSON) |
| `client account create\|list\|rename\|deactivate\|activate` | tenant accounts (needs `accounts`) |
| `client token issue\|list\|revoke\|rotate` | an account's API tokens (secrets shown once) |

## See also

- [17 · The update server](17-update-server.md) — the service on the other end, and how
  to run your own.
- [Threat model](../reference/threat-model.md) — why a compromised server still can't
  forge an update.

---

*[← 15 · The client](15-the-client.md) · [Index](00-introduction.md) · [17 · The update server →](17-update-server.md)*

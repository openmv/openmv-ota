# Operating the fleet

*[← 15 · The client](15-the-client.md) · [Index](00-introduction.md) · [17 · The update server →](17-update-server.md)*

---

Publishing is a moment; operating is the rest of the time. Once a rollout is offering,
the fleet stops being an abstraction: one customer's cameras must stay on last month's
release while everyone else moves, next month's build needs deltas against whatever is
actually running in the field today, the CI machine needs a credential that can publish
but not manage, and at three in the morning you want one command that says whether
anything fell back. This page is that work — the verbs you reach for while a fleet is
live.

## Pins

A pin overrides rollouts for one device or one whole cohort — "this camera runs exactly
this release":

```
openmv-ota client device pin --device-id 30003d000851303436313832 --release-id rel_4f9c2a81d06b73ee
openmv-ota client device pin --device-id 30003d000851303436313832 --clear
openmv-ota client cohort pin --product-id 396486252 --cohort beta --release-id rel_4f9c2a81d06b73ee
```

A device pin beats a cohort pin, and either beats the rollout. A pin only ever produces
an **offer** when it's an upgrade for a settled device; pinning to the version a camera
already runs (or older) simply holds it — no rollout reaches it, nothing downgrades.

`cohort pin` names the product because a cohort can hold devices of **several
products** — your `OPENMV_N6`s and `OPENMV_RT1060`s can all carry the label `beta` —
while the pin carries a release, and a release only fits one product. So pinning
`--product-id 396486252 --cohort beta` freezes only beta's N6 devices; beta's RT1060s
keep following their own rollouts until you pin them too, with the RT release. (Rollouts
work the same way — targeting is always the `(product, cohort)` pair.) A device pin
needs no product: the device id alone is unique.

## Building the next release's deltas

A device patches against **the release it is running**, so a fleet mid-rollout is
spread over several versions, and one delta reaches only the devices on its base. The
deltas must be built **locally** — a delta is named in the *signed* manifest, and the
server never holds signing keys — but only the server knows what the fleet is actually
running. So the release build asks it:

The client asks and fetches, the build stays local — `build` never talks to the
server:

```
$ openmv-ota client release bases --fleet -b OPENMV_N6 --product-id 396486252 -o build/bases
build/bases/OPENMV_N6-base-1.1.0.img.gz  (1.1.0, 358 device(s), 731648 bytes)
build/bases/OPENMV_N6-base-1.0.0.img.gz  (1.0.0, 51 device(s), 730112 bytes)

$ openmv-ota build ota-romfs . --delta-from build/bases   # one delta per fetched base
$ openmv-ota client release publish . -b OPENMV_N6        # uploads the image + every delta
```

`--fleet` warns at fetch time about any group of devices no base can cover (a pruned
release, a republish that split the bytes) — those take the full image, never nothing.
The server can answer because it **retains every published image**: lose the build
directory, re-clone the repo, hand the release to a colleague — the bases are still
there. (`release bases --last N` fetches the N most recent releases instead, when you
want an old image itself rather than the fleet's plan.)

**The first base is the factory image.** A fresh-from-manufacture fleet runs bytes
that were never published, so its first OTA could never be a delta — which is why
`build factory-romfs` also renders its exact bytes in release form,
`build/factory/<board>-ota.img.gz` plus a factory-key-signed manifest. Publish that
pair right after manufacture and the first update ships as a delta too:

```
openmv-ota client release publish . -b OPENMV_N6 -o build/factory
```

(A local build already defaults its delta base to the recorded factory image;
publishing it is what lets `release bases --fleet` cover factory-fresh
devices as well.)

Retention has **no depth limit**: images are small, and only you know how long a version
stays in the field. Reclaiming space is therefore a deliberate act:

```
openmv-ota client release prune --release-id rel_4f9c2a81d06b73ee
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
$ openmv-ota client account create --name "DroneCo"
account acct_7bd21c50e83a94f1 created
admin token (store it now -- not recoverable): 5oQ4wLr8kJ2vN9xB1mA3sT6yD0eF7cH_gPzUiRnE2aM

$ openmv-ota client account list
$ openmv-ota client account rename --account-id acct_7bd21c50e83a94f1 --name "DroneCo GmbH"
account acct_7bd21c50e83a94f1 renamed to DroneCo GmbH

$ openmv-ota client account deactivate --account-id acct_7bd21c50e83a94f1
account acct_7bd21c50e83a94f1 deactivated (3 token(s) revoked)
$ openmv-ota client account activate --account-id acct_7bd21c50e83a94f1
account acct_7bd21c50e83a94f1 activated

$ openmv-ota client token issue --account-id acct_7bd21c50e83a94f1 --name ci --scope publish
token 3f2a9c1e77d0b4a8 issued for acct_7bd21c50e83a94f1
token (store it now -- not recoverable): xK9pW2qL5mR8tV1zC4nB7dF0gJ3hS6yA_eU2iO5rT8wQ

$ openmv-ota client token list --account-id acct_7bd21c50e83a94f1
$ openmv-ota client token revoke <token-hash>
$ openmv-ota client token rotate <token-hash>         # replacement issued, old revoked
```

| about tokens | |
|---|---|
| scopes | `publish` (publish releases), `manage` (rollouts, cohorts, pins, binds), `observe` (read everything), `accounts` (the operator scope). `token issue` defaults to the worker set: publish, manage, observe |
| secrets | shown **once**, at issue/rotate — the server stores only a hash. `token list` shows metadata and hashes, never secrets |
| revocation | by hash. `deactivate` revokes every token an account has and blocks issuing new ones — admin access dies, but fielded devices keep being served, so a billing lapse never bricks a fleet |

**How a device knows its account.** It's baked in at build: you put your account id in
the project (`account_id` under `[product]` in `openmv-ota.toml`), the build stamps it
into the image's `system.json`, and the device reports it with every check-in. On the
first valid check-in the server **learns** that binding and it's sticky from then on —
a later boot reporting a different or empty account (a factory-state fallback, say)
can't move the device. The operator override — the recovery path when a camera was
first seen under the wrong account — (re)binds it to **yours**:

```
$ openmv-ota client device bind --device-id 30003d000851303436313832
device 30003d000851303436313832 bound to acct_7bd21c50e83a94f1
```

Knowing a device id is not owning the device: a binding only controls visibility and
offers, never installs — the camera verifies every image against the keys baked into
its own firmware, so another account's releases can't run on it. An admin-bound device
can't be re-bound by another account (their attempt reads as not-found), and your admin
bind always recovers a wrongly learned one. On the OpenMV-hosted service, who may bind
a given device is additionally gated by proof of ownership; the
[threat model](../reference/threat-model.md) spells out the full trust story.

## Watching the fleet

Four reads cover the whole picture. Each prints the server's JSON verbatim, so what you
see below is exactly what your scripts consume.

### The fleet summary

`client fleet` is the dashboard — the whole fleet in five numbers:

```
$ openmv-ota client fleet
{
  "total": 412,
  "by_version": { "1.2.0": 361, "1.1.0": 51 },
  "by_fallback": { "1.1.0": 358, "unknown": 54 },
  "by_product": { "396486252": 412 },
  "by_cohort": { "__default__": 404, "beta": 8 },
  "fell_back": 2,
  "unconfirmed": 7
}
```

`--product-id` and `--cohort` scope the whole summary — `client fleet --cohort beta`
is the dashboard for exactly the audience a rollout is reaching. At account scope the
`by_product`/`by_cohort` maps structure the totals; note `by_version` then mixes
products, since version strings are per product.

| field | what it answers |
|---|---|
| `by_version` | what the fleet is running |
| `by_fallback` | what it would fall back **to**. A fleet whose devices all have the previous release behind them is in a very different position from one where half report `unknown` — and that is invisible in `by_version` |
| `fell_back` | devices whose last boot rejected a slot — the direct rollout alarm |
| `unconfirmed` | devices mid-trial. They are also the devices deferring further updates until they settle |

`unknown` in `by_fallback` is a device that did not say — a single-image board, which
has no fallback by design.

### The per-device rows

`client device list` is the same picture one camera at a time — everything the device
reported at its last check-in, plus what the server decided about it:

```
$ openmv-ota client device list --cohort beta --limit 1
{
  "devices": [
    {
      "device_id": "30003d000851303436313832",
      "product_id": 396486252,
      "board": "OPENMV_N6",
      "cohort": "beta",
      "current_version": "1.2.0",
      "current_payload_version": 16908288,
      "slot": "A",
      "representation": "ocdl",
      "fallback_reason": null,
      "confirmed": 1,
      "last_offered_release_id": "rel_4f9c2a81d06b73ee",
      "registrar_ref": null,
      "first_seen": "2026-07-02T09:14:55.310221+00:00",
      "last_seen": "2026-08-31T19:38:47.104563+00:00",
      "pinned_release_id": null,
      "account_id": "acct_7bd21c50e83a94f1",
      "streams": "",
      "fallback_payload_version": 16842752,
      "body_sha256": "9c2ff6a1c4f0f9be55b9e9c25e1e6cf1d5f4f34eaa1f5f5f4b2c9c25e1e6cf1d",
      "fallback_version": "1.1.0"
    }
  ],
  "total": 412
}
```

Versions appear twice on purpose: the packed number is what the device reports and what
comparisons use; the decoded form (`current_version`, `fallback_version`) is for you.
Filters: `--product-id`, `--cohort`; pages with `--limit`/`--offset`, and `total` always
counts the whole scoped fleet so a full page is distinguishable from a complete list.
One camera by id, same row shape:

```
openmv-ota client device show --device-id 30003d000851303436313832
```

### The publish history

`client release list` lists what has been published, newest first — the release rows every
rollout and pin points into:

```
$ openmv-ota client release list --limit 1
{
  "releases": [
    {
      "release_id": "rel_4f9c2a81d06b73ee",
      "product_id": 396486252,
      "product": "my-product",
      "version": "1.2.0",
      "payload_version": 16908288,
      "min_platform_version": 84017152,
      "image_sha256": "5d41402abc4b2a76b9719d911017c592a9c25e1e6cf1d5f4f34eaa1f5f5f4b2c",
      "image_size": 1467392,
      "representations": [
        { "format": "full", "url": "OPENMV_N6-ota.img.gz", "size": 734208 },
        { "format": "ocdl", "url": "OPENMV_N6-ota.delta-1.1.0.gz", "size": 51712,
          "base_payload_version": 16842752 }
      ],
      "manifest_key": "manifests/rel_4f9c2a81d06b73ee/manifest.bin",
      "image_key": "artifacts/rel_4f9c2a81d06b73ee/OPENMV_N6-ota.img.gz",
      "delta_key": null,
      "key_id": 256,
      "uploaded_by": "ci",
      "uploaded_at": "2026-08-31T18:11:59.771402+00:00",
      "account_id": "acct_7bd21c50e83a94f1",
      "dev": 0,
      "sbom_key": "sbom/rel_4f9c2a81d06b73ee/sbom.cdx.json"
    }
  ],
  "total": 9
}
```

Worth knowing in there: `dev` marks a release signed with a throwaway `--dev` key
(provenance, visible forever), and `representations` is the signed manifest's own list —
the deltas a device can choose from, each naming the base it patches.

One release by id, and the evidence that shipped with it:

```
$ openmv-ota client release show --release-id rel_4f9c2a81d06b73ee     # the same row, singly

$ openmv-ota client release sbom --release-id rel_4f9c2a81d06b73ee -o sbom.cdx.json
saved sbom.cdx.json (48213 bytes)
```

`release sbom` hands back the CycloneDX SBOM exactly as publish uploaded it — pipe it
to a scanner (no `-o` writes it to stdout) to answer "does the release the fleet runs
carry this CVE?".

### The audit log

`client audit` is the append-only record of every admin action — publish, rollout
changes (auto-pauses included), pins, assignments, renames, binds, account and token
events — each with its actor, and each entry hash-chained to the one before, so
tampering with history is detectable:

```
$ openmv-ota client audit --since 41
{
  "events": [
    {
      "seq": 42,
      "ts": "2026-08-31T19:40:11.905122+00:00",
      "actor": "system",
      "action": "rollout.autopause",
      "entity_type": "rollout",
      "entity_id": "ro_1c3f88ba90d2e644",
      "data": { "failures": 3, "attempted": 41 },
      "prev_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "entry_hash": "2c26b46b68ffc68ff99b453c1d30413413422d706483bfa0f98a5e886266e7ae",
      "account_id": "acct_7bd21c50e83a94f1"
    }
  ]
}
```

It pages by `--since SEQ` — a sequence **cursor**, not an offset — so a poller resumes
exactly where it left off and never skips or repeats entries when new ones land
mid-page.

## Scripting: `--json`

Every verb takes `--json`, which prints the server's response verbatim instead of the
summary line — so nothing has to be scripted by parsing English:

```bash
rel=$(openmv-ota client release publish ./p -b OPENMV_N6 --json | jq -r .release_id)
tok=$(openmv-ota client token issue --account-id "$acct" --name ci --json | jq -r .token)
```

Verbatim matters most for the one-time secrets (`account create`, `token issue`,
`token rotate`): the token exists in exactly that one response, and a script that can't
capture it has to mint another. `publish --percent` is two API calls, so its JSON nests
the rollout under `rollout` and leaves the release fields where a plain `publish` puts
them — no special case for parsers.

---

*[← 15 · The client](15-the-client.md) · [Index](00-introduction.md) · [17 · The update server →](17-update-server.md)*

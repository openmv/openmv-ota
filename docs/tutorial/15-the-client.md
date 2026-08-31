# The client

*[← 14 · Recovery](14-recovery.md) · [Index](00-introduction.md) · [16 · Operating the fleet →](16-operating-the-fleet.md)*

---

`openmv-ota build ota-romfs` leaves a signed release in `build/`; nothing so far decides
which camera downloads it. That is the **update server**'s job — a central service that
hosts your releases and stages them across the fleet — and **`openmv-ota client`** is how
you drive it. Everything the client does goes through the server's admin HTTP API,
so anything it can do, your own scripts and dashboards can do too.

This page is the release workflow: log in once, publish, stage it out ([16 ·
Operating the fleet](16-operating-the-fleet.md) covers watching it land and the rest of
the remote surface). You need two things — a **server URL** and an **admin token**.
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
| `https://ota.cloud.openmv.io` | the built-in fallback for the server URL — the OpenMV-hosted service |

Only the URL has a built-in fallback; the token is always yours to provide.
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
- **a state** — `active`, `paused`, or `stopped`;
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

Which devices make up that percentage isn't a choice you make or a list the server
keeps. Think of it as a lottery: for this rollout, every device holds a fixed ticket
number between 0 and 9,999, computed by hashing the rollout id together with the
device id —

```
ticket = sha256(rollout_id + ":" + device_id) % 10000     # 0–9999: deterministic,
                                                          # NOT unique per device
staged = ticket < percent * 100                           # 5%  -> tickets 0–499
                                                          # 50% -> tickets 0–4999
```

— and the rollout simply admits every ticket below the bar its percent sets. That one
idea buys three properties:

- **Stable**: the hash gives the same ticket on every poll, so a device never flips in
  and out of the rollout — with nothing stored per device to make it so.
- **Monotonic**: raising the percent only lowers the bar, so it only ever **adds**
  devices; everyone already offered stays offered. This is the "raise it as confidence
  grows" model.
- **Reshuffled per rollout**: the rollout id is part of the hash, so every rollout
  deals fresh tickets — the same camera isn't the canary every time.

Tickets aren't seats — every device gets its **own draw**, but there are only 10,000
possible values, so different devices land on the **same number** (in a fleet of
100,000, about ten per value). That's fine, because only the fraction below the bar
matters: a 5% rollout stages ~5% of the cohort at any fleet size, and the bigger the
fleet, the closer the real fraction lands to the dial. The ticket count only sets the
resolution — the finest slice is one ticket, 0.01% of the cohort.

From there the lifecycle is four actions:

```
openmv-ota client rollout raise 50 --rollout-id ro_1c3f88ba90d2e644
openmv-ota client rollout pause  --rollout-id ro_1c3f88ba90d2e644
openmv-ota client rollout resume --rollout-id ro_1c3f88ba90d2e644
openmv-ota client rollout stop --rollout-id ro_1c3f88ba90d2e644
```

| action | what happens |
|---|---|
| `raise N` | widen the staged slice to N percent. Percent is **monotonic** — lowering it is refused, because devices already offered the release can't be un-offered |
| `pause` | stop offering; resumable. The server also **auto-pauses** a rollout whose fallback rate among offered devices crosses its failure threshold (5% by default) and records an audit event |
| `resume` | start offering again |
| `stop` | stop offering **permanently** (a stopped rollout can't be resumed — create a new one). Devices that already took the release keep it — the server never downgrades a camera; the device's own anti-rollback wouldn't accept one anyway |

`client rollouts` lists them — so a lost id is always recoverable, and each row
carries `cohort_devices`, the audience its percent applies to:

```
$ openmv-ota client rollouts
{
  "rollouts": [
    {
      "rollout_id": "ro_1c3f88ba90d2e644",
      "release_id": "rel_4f9c2a81d06b73ee",
      "product_id": 396486252,
      "cohort": "beta",
      "percent": 5.0,
      "state": "active",
      "failure_threshold": 0.05,
      "attempted": 21,
      "updated": 19,
      "failures": 0,
      "created_at": "2026-08-31T18:12:04.281937+00:00",
      "updated_at": "2026-08-31T19:40:11.905122+00:00",
      "account_id": "acct_7bd21c50e83a94f1",
      "cohort_devices": 412
    }
  ],
  "total": 1
}
```

`client rollout status --rollout-id` reads one rollout's score:

```
$ openmv-ota client rollout status --rollout-id ro_1c3f88ba90d2e644
{
  "rollout_id": "ro_1c3f88ba90d2e644",
  "state": "active",
  "percent": 5.0,
  "cohort_devices": 412,
  "staged_devices": 21,
  "attempted": 21,
  "updated": 19,
  "failures": 0,
  "rates": { "attempted": 1.0, "updated": 0.9047619047619048, "failures": 0.0 },
  "reported": { "installed": 19, "failed": 0 }
}
```

`staged_devices` is the current target — `percent` of the audience (an estimate:
membership is a hash, not a list) — and `rates` reads each counter against it, so
"how far through this stage is the fleet, and how is it going" is one glance:
everyone staged was offered it, 90% already run it, nobody fell back. Time to raise.

## See also

- [16 · Operating the fleet](16-operating-the-fleet.md) — watching devices take it,
  pins, delta bases, accounts, and the full verb table.
- [8 · Release artifacts](08-release-artifacts.md) — what `publish` uploads and how it's signed.

---

*[← 14 · Recovery](14-recovery.md) · [Index](00-introduction.md) · [16 · Operating the fleet →](16-operating-the-fleet.md)*

# Cohorts and rollouts

*[← 15 · The client](15-the-client.md) · [Index](00-introduction.md) · [17 · Operating the fleet →](17-operating-the-fleet.md)*

---

A published release is inert until something offers it. Two ideas decide who gets
what: **cohorts** — how you group devices — and **rollouts** — how a release reaches
a growing share of one group.

## Cohorts

Before staging an update, decide who gets it first. **Cohorts** exist so a release
can go to less than everyone — a `beta` bench, a canary site, a customer. How they fit
the rest of your account, top to bottom:

- Your **account** holds everything — devices, releases, rollouts, cohorts, audit.
- **Each board you build for is its own product**: `project new` derives one product id
  per board and stamps it into `openmv-ota.toml` ([page 2](02-projects.md)); a release
  is built and published per board (`publish -b`) — which is what keeps an `OPENMV_N6`
  image from ever being offered to an `OPENMV_RT1060`.
- **Cohort names are free-form labels on devices** — every device is in exactly one,
  starting in `__default__` until you move it, and the same name can span products.
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
    { "cohort": "__default__", "devices": 404, "by_product": { "396486252": 404 } },
    { "cohort": "beta", "devices": 8, "by_product": { "396486252": 8 } }
  ]
}

$ openmv-ota client cohort assign --cohort beta --product-id 396486252
assigned 412 device(s) (product 396486252) to cohort beta
```

`assign` takes exactly one selector: `--device-id` (repeatable) moves those exact
devices; `--product-id` moves every device of the product. `client device list` lists both
ids per device. Assignment is also removal — a device moved to `beta` leaves
`__default__` — and it counts only the devices that exist and are yours: the `1/1` in
the summary is what makes a typo'd id visible.

The rest of a label's lifecycle — a name springs into being on first `assign`,
`rename` relabels it everywhere at once (devices, rollouts, pins — a mid-flight
rollout keeps its audience under the new name), and `delete` retires it:

```
$ openmv-ota client cohort rename --cohort beta --name pilot
cohort beta renamed to pilot (412 device(s), 1 rollout(s), 1 pin(s))

$ openmv-ota client cohort delete --cohort pilot
cohort pilot deleted (412 device(s) back to __default__, 1 pin(s) dropped)
```

Renaming onto a name already in use is refused (merging is `assign`), delete is
refused while an active rollout still targets the cohort, and `__default__` can be
neither renamed nor deleted.

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
$ openmv-ota client release publish . -b OPENMV_N6 --cohort beta --percent 5
published rel_4f9c2a81d06b73ee  version 1.2.0  (full, ocdl)
rollout ro_1c3f88ba90d2e644  5.0%  cohort=beta

$ openmv-ota client rollout create --release-id rel_4f9c2a81d06b73ee --cohort beta --percent 5
rollout ro_1c3f88ba90d2e644  5.0%  cohort=beta
```

(`--percent` alone stages to `__default__`; on `publish`, `--percent` is what triggers
staging at all — without it the release is published and left inert.)

Which devices make up that percentage isn't a choice you make or a list the server
keeps. Think of it as a lottery: for this rollout, every device holds a fixed ticket
number between 0 and 999,999, computed by hashing the rollout id together with the
device id —

```
ticket = sha256(rollout_id + ":" + device_id) % 1000000   # 0–999999, deterministic
staged = ticket < percent * 10000                         # 5%  -> tickets below  50,000
                                                          # 50% -> tickets below 500,000
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

The percent is a **fraction of the cohort, at any fleet size**: 15,000 devices at 5%
stages about 750 of them. The draw is random, so the count wobbles slightly around the
dial — `rollout status` shows the real numbers.

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

`client rollout list` lists them — so a lost id is always recoverable, and each row
carries `cohort_devices`, the audience its percent applies to:

```
$ openmv-ota client rollout list
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

---

*[← 15 · The client](15-the-client.md) · [Index](00-introduction.md) · [17 · Operating the fleet →](17-operating-the-fleet.md)*

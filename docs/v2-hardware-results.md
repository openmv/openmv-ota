# v2 on hardware — the first full sweep

Run 2026-08-08/09 across the four boards available on the bench (RT1060 and AE3 were in use by
someone else and were not touched). Every leg ran the board's own regression set from
`ci/hil/regression_scenarios`, one scenario at a time, from a fresh provision.

## Results

| board | interface | result |
|---|---|---|
| **OpenMV N6** | lan | **11/11 PASS** — the complete set, including `watchdog`, `watchdog_bite` and `reinstall` |
| **OpenMV N6** | wifi | **PASS** (`delta`, the regression's secondary-interface set) |
| **Arduino Nicla Vision** | wifi | **9/9 PASS, twice** — run a second time to prove it was stable, not lucky |
| **Arduino Portenta H7** | wifi | **9/9 PASS** |
| **Arduino Portenta H7** | lan | **PASS** (`delta`) |
| **OpenMV H7 Plus** | wifi | `full` **PASS**; every delta-based scenario timed out — **root-caused and fixed**, see [h7plus-xip-tail-wedge.md](h7plus-xip-tail-wedge.md) |

## What the sweep found

Three real bugs, all invisible to the host suite, all fixed. Every one of them is a **silent**
failure — no fault, no exception, no log — which is the shape this whole exercise exists to
catch, and the reason the sweep is worth its wall-clock.

1. **A silent HardFault on the N6** (`72930c5`). v2 added a one-byte attempt marker — the only
   odd-sized flash write in the tree. The N6's XSPI runs octal DTR (two bytes per clock), so a
   single-byte program hard faults: no log, no exception, no reset, the board just stops and
   drops off USB, on the **first boot of every trial**. Now a 16-byte marker, the portable unit
   (one AE3 MRAM write, what every other marker already uses).

2. **A/B lost its fallback after every successful update** (`dfa6c83`). `confirm()` raises the
   rollback floor to the running version, so the slot behind an accepted update is below the
   floor *by construction* — and the boot-time anti-rollback check was rejecting it. A Nicla
   confirmed 1.1.0 and immediately logged `boot: rejected A:rollback`. Every device would have
   lost its fallback on its first successful update, which is to say A/B would have bought
   nothing. A **confirmed** slot is now exempt at boot; the floor still gates installs and any
   unconfirmed slot, which is where a replayed release actually arrives.

3. **A bulk XIP read that reaches the end of the QSPI wedges the H7 Plus** (`959c463`). v2 puts
   slot B at the end of the partition, and on that board the partition ends exactly at the end of
   its 32 MiB QSPI — so the erase-verify's last read wedges the QUADSPI and every later
   memory-mapped read hangs forever. `full` survived on luck; `delta` reads its patch base
   straight off XIP and died there. Fixed with a 512-byte tail guard on every XIP alias. Full
   write-up, including the two wrong conclusions it cost, in
   [h7plus-xip-tail-wedge.md](h7plus-xip-tail-wedge.md).

Plus one harness bug (`153fcca`): `_tamper` selected artifacts by the v1 name `*.delta.gz`, so
under v2's per-base naming it corrupted the wrong file and the device installed the untouched
delta. It now tampers every representation.

## Accumulation across generations

Scenarios each start from a fresh provision, so none of them exercise what a device does over
its *life*. Run separately on the Nicla, three updates with no re-flash between them:

| round | running | fallback | install counter |
|---|---|---|---|
| 2.1.0 | **A** 2.1.0 | B 1.4.0 | 4 |
| 2.2.0 | **B** 2.2.0 | A 2.1.0 | 5 |
| 2.3.0 | **A** 2.3.0 | B 2.2.0 | 6 |

Slots alternate A→B→A, the counter climbs monotonically, and **every round leaves the previous
release as a confirmed, bootable fallback** — each of them below the floor at the time. That
last column is fix (2) holding across three generations.

## Behaviour worth knowing about, which is not a bug

Twice a board was reset while an install was in flight (the harness resets to open a scored
window), and both times the log read:

```
install: erasing B (2097152 bytes)
log: configured            <- reset MID-ERASE
boot: rejected B:body-sha -> mounted A
```

A half-written slot, correctly rejected, previous release booted. That is the anti-brick path
working on real hardware — but it emits `boot.fallback`, which the happy-path scenarios forbid,
so the scenario fails. **The harness should settle the device before opening its scored
window**; until it does, a happy-path leg can flake this way (~1 in 14 runs observed). Not
changed here, because weakening the `boot.fallback` assertion would hide genuine fallbacks.

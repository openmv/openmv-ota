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

## The 2026-08-12 sweep — the whole fleet, in CI, on the merge gate

Three consecutive full-fleet runs on the PR gate (nine legs, every board's own regression set).
This is the first sweep where **every board was reachable**, the AE3 included.

| board | interface | scenarios | result |
|---|---|---|---|
| **OpenMV N6** | lan | 11 | **PASS** — incl. `watchdog`, `watchdog_bite`, `reinstall` |
| **OpenMV N6** | wifi | 1 | **PASS** |
| **OpenMV RT1062** | lan | 11 | **PASS** — incl. `reinstall`, `watchdog`, `no_slot` |
| **OpenMV RT1062** | wifi | 1 | **PASS** |
| **OpenMV H7 Plus** | wifi | 9 | **PASS** |
| **Arduino Nicla Vision** | wifi | 9 | **PASS** |
| **Arduino Portenta H7** | lan | 1 | **PASS** |
| **Arduino Portenta H7** | wifi | 9 | **PASS** on runs 1-2; run 3 failed `delta` in CI and **passes on the bench** (359 s) — intermittent, see below |
| **OpenMV AE3** | wifi | 2 | **PASS** |

### Every failure this sweep found was in the HARNESS, not the device

Worth stating plainly, because it is the opposite of what the red legs looked like. Four bugs, all
of them the test rig making a timing or observability assumption the device had outgrown:

1. **A faster install outran a polled observation.** The blank-skip erase cut the RT1062's slot
   erase from 54 s to 4 s, and `saw_golden` — which had to *catch* the device reporting the old
   version on a 15 s poll — stopped latching. The leg then burned its whole window and failed an
   install that was flawless (`delta`, `watchdog`: 362 s PASS -> 606 s FAIL). Fixed by scoring the
   promoted path on the in-window markers, which are emitted evidence rather than sampled state.

2. **The update went live before the board was reset.** `run_cycle` opens each window with a hard
   reset; `reinstall` phase 2 published *before* that call, so the device (polling every ~5 s) could
   start installing in the gap and be reset MID-ERASE — leaving a half-written slot, an unsettled
   device the server rightly refuses to offer to, and a deadlocked phase. A pure race: it passed on
   the bench and failed in CI on identical code, so it is closed by ordering, not by a delay.

3. **`no_slot` erased its own instrumentation.** `.hilcov_uart` is baked into the ROMFS (the one
   volume that survives an armed watchdog) and that scenario's brick erases the whole romfs region,
   so the act creating the condition under test also removed the file naming the marker UART. The
   device printed `boot: no bootable slot` correctly into a channel nobody was listening to. Reading
   the console instead does *not* work — boot.py prints before the CDC enumerates, proven by an SWD
   reset that produced 0 bytes in 38 s — so the brick now seeds `/flash/.hilcov_uart` first, which
   the erase spares. 1030 s FAIL -> 136 s PASS.

4. **A finished run thrown away by a missing directory.** The trace is written last, so a
   non-existent parent turned a completed, correct OTA into a non-zero exit with no RESULT line —
   indistinguishable from a scenario failure. The write now creates its directory.

The general lesson, and the one worth carrying: **any assertion that must *catch* a transient state
is a latent race against the device getting faster.** Prefer evidence the device emits — markers,
logged once, captured in a reset window — over state the harness has to sample at the right moment.
And when a leg's failure *signature* changes, that is information: the old cause is fixed and a new
one has taken its place.

## The re-sweep, after the XIP tail guard

Bug 3 below is in the *shared* XIP read path, not an H7-Plus-only patch, so the whole fleet was
re-run against it rather than just the board that exposed it:

| board | interface | result |
|---|---|---|
| **OpenMV H7 Plus** | wifi | **9/9 PASS** — was 1/9. delta 293s, full 362s, rollback 311s, corrupt 342s, corrupt_sha 339s, bad_sig 252s, bad_key 243s, bad_version 242s, reinstall 434s |
| **Arduino Nicla Vision** | wifi | **9/9 PASS** |
| **OpenMV N6** | lan | **11/11 PASS** — including `watchdog` and `watchdog_bite` |
| **OpenMV N6** | wifi | **PASS** (`delta`) |
| **Arduino Portenta H7** | wifi | **9/9 PASS** (`bad_sig` re-run after the own goal below; also passes over lan, 315s) |
| **Arduino Portenta H7** | lan | **PASS** (`delta`) |
| **OpenMV RT1062** | lan | **11/11 PASS** — delta 362s, full 447s, rollback 403s, corrupt 463s, corrupt_sha 432s, bad_sig 298s, bad_key 298s, bad_version 297s, reinstall 532s, watchdog 363s, no_slot 137s |
| **OpenMV RT1062** | wifi | **PASS** (`delta`, 358s) |
| **OpenMV AE3** | wifi | **NOT RUN** — its node has no coverage-UART bridge attached, see below |

Every scenario on every *reachable* board passes with the guard in place. The RT1062 matters
here beyond being another board: it is the **block-device** port, so it exercises the one part of
this change that is not XIP-specific — the write-verify now compares the prefix it read back, and
on that port the readback is never short, so the slice must never run.

### What the PR gate caught that the bench sweep could not

The branch's first CI run (PR #62) failed six checks, and **none of them were device code** —
this branch had never had CI run against it, only the host suite locally, so several checks were
still asserting v1 behaviour:

- `build OPENMV2/3/4` asserted `project new --ota` is REFUSED as "not OTA-capable". v2 flipped
  exactly that: a one-slot partition now builds in single-image mode. The command still failed,
  just on the next error down (the signing-key passphrase), which is why it read as a build break.
- The QEMU `trial_failed` case spent attempts at the **pre-16-byte offset**, so it handed in a
  trial that was not actually spent and had been asserting nothing since the N6 hard-fault fix.
- The QEMU `rollback` case expected a **confirmed** slot below the floor to be rejected — the very
  behaviour that cost the fleet its fallback, and which `dfa6c83` deliberately reversed.
- `ota-cycle ARDUINO_NICLA_VISION wifi` failed on an **orphaned bench server** left holding
  port 8443 by a hand-run leg killed without its child. See `bench_server._assert_is_our_server`.

Worth drawing the lesson: a green bench sweep is not a green gate. The sweep runs one scenario at
a time against a hand-launched harness; the gate runs the *checks*, and the checks are where the
stale assumptions live. Run CI on a long-lived branch early, not once at the end.

### The AE3 is not covered, and it is the board that most needs to be

`OPENMV_AE3 wifi` could not be run: the node has no `/dev/ttyUSB*` coverage-UART bridge attached
(a documented node fixture — see `ci/hil/NODE_REQUIREMENTS.md`), so every leg dies in 2s with
`could not open port /dev/ttyUSB0`. No CP210x has enumerated on that node in its uptime. Its CI
runner is online, so the PR gate's AE3 leg will run and fail until the adapter is reconnected.

This is worth stating plainly rather than filing as an environment nit: the AE3 is an **Alif XIP
port**, and `_XIP_TAIL_GUARD` changes exactly the XIP read path. Whether its romfs partition also
ends flush against the end of its flash decides whether it shared the H7 Plus's silent wedge. Of
every board in the fleet it is the one whose result carries the most information about this
change, and it is the one we do not have.

### A wifi "outage" that was an own goal, worth writing down

Partway through the re-sweep several wifi legs began failing with the device booting, getting its
`device_id`, and then never reaching `network up, starting run()` — no check-in, no offer, no
marker past boot. It looked exactly like a bench outage, and was diagnosed twice as one (first as
the AP being down, then as DHCP lease exhaustion), on evidence that seemed to fit: two cyw43
boards failing while the WINC-based H7 Plus kept passing, and the same board's lan leg passing.

**None of that was true.** Asked directly, the board associates and gets a lease in 5–7 seconds:

```
CONN t=5s status=3 connected=True
CONN ifconfig: ('192.168.0.158', '255.255.255.0', '192.168.0.1', '192.168.0.1')
```

The failing runs were launched by ad-hoc one-off scripts that did not `. "$HOME/.hil-env"`, so
`WIFI_SSID`/`WIFI_PASSWORD` were unset in the environment `ota_cycle.py` ran in, and
`bench_main_py` baked an **empty SSID** into the golden app. The device then sat in
`while not wl.isconnected()` forever, exactly like a dead AP. Every leg that passed was launched
by `regress_generic.sh`, which does source it; every leg that "failed" was not — and the one
apparent counter-example, `bad_sig` passing over lan, passes because lan needs no credentials at
all.

Two lessons, both cheap:

- **The correlation that looked causal (cyw43 vs WINC) was an artifact of which launcher each
  board happened to be run from.** Two independent boards failing the same way is not proof of an
  environmental cause.
- **Ask the device.** One REPL session — scan, connect, print `status()` — settled in two minutes
  what two rounds of inference got wrong. The AP was visible at RSSI −25 the whole time.

Worth fixing on the harness side regardless: the bench app's bring-up is
`while not wl.isconnected(): sleep_ms(200)` with no timeout, and it does not check that the SSID
it was handed is non-empty. Either one would have turned this into an immediate, legible failure
instead of a 1200 s timeout with nothing logged.

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

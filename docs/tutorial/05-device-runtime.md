# The on-device OTA runtime

*[← 4 · Flashing](04-flashing.md) · [Index](00-introduction.md) · [6 · The update server →](06-update-server.md)*

---

OTA has two pieces that run **on the camera** (everything else — `project`, `build`,
signing — runs on your host):

1. **`boot.py`** — frozen into the firmware by `build firmware` for an OTA project. At
   boot it picks and verifies which image to run.
2. **`openmv_ota`** — a small Python library scaffolded into your project's
   `app/lib/openmv_ota/` by `project new --ota` and packed into the ROMFS at
   `/rom/lib/openmv_ota/`. Your app imports it to complete an update and to apply
   bundled resources.

`boot.py` decides *what runs*; `openmv_ota` lets the running app *commit the update*
and *write helper resources*. Both are self-contained (they can't import the host
`openmv_ota.ota.*` packages under MicroPython), and the pure logic of each is
host-tested while the device I/O is exercised under QEMU — see [ci.md](../reference/ci.md).

## `boot.py` — slot selection at boot

An OTA partition holds **two slots of equal size, A and B**. Neither is special: both are
real, signed, updatable images, and the one that boots is simply the newest valid one. On
every boot `boot.py` runs after the board's stock `_boot.py` and, **for each slot**:

1. Reads the slot's signed [trailer](../reference/trailer.md) and **verifies the ECDSA signature**
   (via the on-device mbedtls shim) *before trusting any header field*.
2. Checks the authenticated header: **integrity** (body SHA-256), **cross-flash guard**
   (`product_id`), **compatibility** (`min_platform_version`), and **anti-rollback**
   (`payload_version` vs the **rollback floor** — see below).
3. Applies the **trial rules** (below).

Then it mounts the surviving slot with the **highest install counter** at `/rom`. It
records the outcome in module globals (`last_slot`, `last_payload_version`,
`last_failure_reason`) for the app to read, since the boot path can't print.

So a bad, corrupt, mis-targeted, downgraded, or un-confirmable update can never strand the
device: it simply is not the newest *valid* slot, and the previous release — which is still
sitting in the other slot, untouched — runs instead. If **no** slot survives there is no
factory image to retreat to; the device hands off to firmware-resident recovery, which
re-downloads until it has a working image.

**Ordering is an install counter, not the version.** Each install stamps a
`u32 ‖ ~u32` counter (self-validating, so a torn write reads as *unknown* rather than as
some other number) into the slot it writes, one higher than anything present. The version
cannot do this job: installing the *same* version twice is legitimate — a reinstall, a
re-flash — and must not be ambiguous. The version stays a human-facing fact and an
anti-rollback input, never a boot-order input. A slot whose counter is unreadable is still
bootable if it verifies; it just sorts last, because we cannot claim it is newer than
something that says so.

**The anti-rollback floor** is a monotonic minimum version, so a device can't be downgraded
to an *older signed* release (a replay attack — the signature is genuine, just stale). It
starts at the provisioned version and **advances**: each `confirm()` appends the running
version to an append-only log in the running slot's `rollback` sector (a 1→0 flash program,
no erase — a power loss mid-append just leaves an ignored torn entry; when the fixed-size
log fills, the floor simply freezes at its max). `boot.py` takes the highest version logged
across **both** slots as the floor, and every install **copies the current floor forward**
into the slot it writes — without that, rewriting whichever slot happened to hold the
highest entry would silently lower the floor and re-admit a release the device had moved
past. The floor is raised *before* `CONFIRMED` is written, so a crash in between falls back
safely (the floor never locks out the image behind it). Each slot reserves four control
sectors — `spare`, `rollback`, `status`, `trailer` — 4 KiB each, in both A/B and
single-image mode, so there is one on-flash shape rather than two.

### Single-image mode

Boards whose entire ROMFS is a single erase sector (OpenMV2/3/4) cannot host two slots, so
they build in **single-image mode**: one slot spanning the partition, no fallback. Erasing
"the target slot" there *is* destroying the running image, which is why firmware-resident
recovery is the enabling piece rather than a nicety — a failed update costs a network round
trip instead of a reboot, and a device that cannot reach the network needs a physical
reflash. Everywhere else A/B is the default and single-image is an explicit opt-out
(`single_image = true`), named for what you get.

## The update lifecycle (and your app's one job)

An installed image is on **trial** until your app says otherwise. Markers in the slot's
status sector drive it, plus an **attempt region** — one byte consumed per trial boot:

```
updater writes the OTHER slot, stamps the counter, sets `pending`  (the running image is untouched)
        │
   boot 1 ─ boot.py: newest slot, pending, attempts left → consume one, mount it   (on trial)
        │
   your app runs, validates itself healthy → openmv_ota.confirm()   → `confirmed`
        │
   later boots: pending+confirmed → mount it                        (committed)

   …but if the trial image hangs/crashes BEFORE confirm():
   boots 2, 3 ─ same again, one attempt each
   boot 4 ─ boot.py: attempts exhausted, never confirmed → reject it → mount the other slot
```

So **your app must call `openmv_ota.confirm()` once it has proven itself healthy** —
otherwise the trial eventually gives up and the device returns to the previous release.
Confirm *after* a real health check (sensors up, first frame, your self-test), **not**
blindly at boot, or you defeat the rollback safety.

**A trial gets `max_attempts` boots (default 3), not one.** The costs are lopsided: a false
rejection costs a full re-download, erase and write that the server then offers again —
minutes, traffic and flash wear, repeatedly — while an extra attempt on a genuinely bad
image costs one reboot. The honest limit, and why the default stays low: retries only help a
failure that *self-resets*. A **hang** now hangs N times instead of once. Set
`[ota].max_attempts = 1` for v1's single-shot behaviour.

The attempt is recorded **before** the image runs, which is what makes a hang count. Two
subtleties follow from it: if `boot.py` cannot record the attempt (the write fails or won't
verify) it does **not** run that slot — an untracked trial could hang forever with no way to
know to move on — and it drops to the next-newest slot rather than abandoning the boot.

**Updates are deferred while a trial is unconfirmed.** The installer writes the slot you are
*not* running, which during a trial is the last release known to work. Taking a new update
then would trade a proven fallback for an unproven one, at the moment the device has already
said it is unsure of itself — so an offered update waits until you `confirm()`. (Single-image
devices are exempt: there is no fallback to protect, and waiting would strand them.)

## `openmv_ota` — the runtime library

`project new --ota` scaffolds `app/lib/openmv_ota/` (plain Python you own and can
extend); `build romfs` compiles + packs it to `/rom/lib/openmv_ota/`. It exposes:

- **`status()`** — read-only view of what boot.py did this boot (it mirrors its result
  onto `_ota_config`, the module the lib reads — importing boot.py would re-run it):
  - `slot` — `'A'` | `'B'` | `None` (which slot booted),
  - `fallback_reason` — why the *other* slot was rejected, or `None`. A reason here means
    **the last update failed and you are running the previous release** — worth reporting
    upstream,
  - `payload_version` — the booted image's version,
  - `representation` — `'full'` | `'delta'` | `None` — how this image was installed
    (the updater stamps this; `None` for a provisioned image). Lets you see on-device
    whether deltas are actually being applied,
  - `pending` / `tried` / `confirmed` / `trial` — the running slot's trial state.
    (`tried` is a v1 leftover kept for compatibility; v2 counts attempts instead.)
- **`slots()`** — every slot, newest first: `slot`, `running`, `payload_version`,
  `counter`, `confirmed`, `pending`. This is the half `status()` cannot tell you — **what
  the device would fall back to** — and it is what the check-in reports so a fleet operator
  can see it. One entry in single-image mode.
- **`identity()`** — the running image's identity/provenance from `/rom/system.json`
  (`board`, `product`, `product_id`, `app_version`, `vendor`, toolchain, …) plus `device_id`
  (this unit's hardware id from `machine.unique_id()`) — what an update server reads to
  decide what to push, and to address the specific device. `{}` if there's no system.json.
- **`confirm()`** — keep the running image: **advances the anti-rollback floor** to this
  version, then writes `confirmed` into **the slot you are running** — iff it is an
  un-confirmed trial, else a no-op. Idempotent (safe to call every boot once healthy),
  returns whether it just confirmed. Everything is addressed by the running slot, so there
  is no way to confirm a trial you fell back *from* — the guard is structural rather than a
  slot-name check. Confirming also **ends the deferral**: an update offered while the trial
  was unproven is taken on the next poll.
- **`sync()`** — apply any **bundled resources** (see below) whose on-device target
  differs from the bundled copy. A flash erase + chunked write of a whole partition, so
  **not quick** — it feeds the watchdog (`openmv_wdt`) the same minimal way `install()`
  does (`relax()` around the erase, `feed()` per chunk, including the already-applied
  re-read). Idempotent, returns the names applied; a no-op when nothing is bundled. Call
  it **early**, before a resource's consumer is used (e.g. before the helper core runs).
- **`install(url, ca=None)`** — download a gzipped slot image over HTTPS and install
  it (see [Installing an update](#installing-an-update-install) below). Does **not** return
  on success — it reboots into the new image's trial.

Both report their progress, **logged at every 10% step** (`install: 40% (…)`,
`sync coprocessor: 70% (…)`), so an enabled logger shows movement through the long flash
write without a line per 4 KiB chunk.

```python
import openmv_ota

openmv_ota.sync()                 # early: bring bundled resources (e.g. the helper
                                  # core's romfs) up to date with this image
# ... start your app; once it has validated itself healthy:
openmv_ota.confirm()              # keep this update (no-op unless it's a trial)

st = openmv_ota.status()
if st["fallback_reason"]:         # the last update failed and we fell back -> report it
    report_to_server(openmv_ota.identity(), st["fallback_reason"])
```

Both `confirm()` and `sync()` **read their flash writes back and compare** (not just
trust the return code) and **raise `OSError`** if a write is rejected or doesn't take,
so a failed update surfaces instead of passing silently — wrap them in `try`/`except`
if you want to react.

## Installing an update (`install()`)

`install(url, ca=None)` is the on-device piece that fetches and applies an update.
`url` is the **signed manifest** URL (`build ota-romfs`), *not* a raw image — the device
resolves the actual image from the manifest itself (representation URLs are relative to the
manifest's URL by default). Something else decides *which* manifest URL to hand it (how
that's obtained is out of scope here). It:

1. Opens an **HTTPS** connection (plaintext HTTP is refused), verifying the server
   against `ca` with `CERT_REQUIRED` + SNI — all **before** erasing anything.
2. **Fetches + verifies the manifest** (into RAM): checks its ECDSA signature against the
   same frozen trusted keys as an image trailer, then applies the device-relative checks
   — `product_id` cross-flash guard, `min_platform_version`, and the **anti-rollback floor**
   (the highest version any slot has recorded) — exactly mirroring what `boot.py` enforces
   on the image, just *earlier*. Any failure here raises with `/rom` intact.
3. **Selects a representation** from the manifest — the **full** image, or a **delta**
   when one is offered whose base matches the version this device is *running* and it's
   smaller — and opens a second HTTPS GET for it. (Single-image devices never take a delta:
   the base would be the very slot about to be erased.)
4. **Picks its target slot — the one the device is not running** — and erases it, then
   **streams** the image straight in. For a full image: decompress a chunk → write → **read
   back and compare** → repeat, skipping erased `0xFF` runs. For a delta: stream-decompress
   the patch and reconstruct against the **running** slot (copy a run from it + add the
   patch's per-byte difference, vectorised with `ulab`; the patch is never held whole in
   RAM), writing+verifying the same way. Either way the stream is hashed and checked against
   the manifest's reconstructed-image **sha256** (fail-fast). A ~1 MB image is never held in
   RAM. Handles `Content-Length`, chunked, close-delimited responses, and redirects.
5. Arms the slot **last**, only after the whole image verified: the representation marker,
   the carried-forward rollback floor, the install counter, and `pending` — in that order,
   so a slot that dies partway is either not bootable or fully described. Then reboots into
   the trial (your app calls `confirm()` once healthy).

**It does not return on success — it reboots.** Two consequences:

- **Call it last.** It reboots on success, so nothing after it runs. Bring the network up,
  do any teardown, *then* call `install()`. (The installer runs from RAM — `install()` reads
  `data/installer.py` and `exec`s it — which is what makes single-image mode possible at all,
  since there the erased slot *is* the one the running app executes from.)
- **Failure is safe.** A pre-flight failure (bad URL, DNS, TLS, HTTP status) raises
  **before** the erase, with `/rom` intact, so you can catch it and retry without a reboot.
  A failure *after* the erase reboots, and boot.py rejects the half-written slot (bad
  signature/hash) and mounts the previous release; `status()` then reports the fallback so
  you know the update failed. Under A/B that previous release is the last update that
  worked — not a years-old factory build.

```python
import network, openmv_ota
# ... bring up WiFi / Ethernet / WiFi-HaLow, then:
try:
    openmv_ota.install("https://downloads.example.com/fw/OPENMV_N6-manifest.bin")
    # unreachable on success — the device reboots into the trial
    # (progress is logged at each 10% step; no callback — the app is being erased)
except OSError as e:
    print("update failed, still running the current image:", e)
```

**TLS trust.** `ca` is the PEM trust store: `None` (the default) reads the bundled
`data/ca.pem`, `bytes` are used directly, a `str` is a path. `project new` downloads a
fresh Mozilla root bundle into `data/ca.pem` so common public CAs (incl. the ones
Cloudflare R2 rotates among) verify out of the box; replace it with your own provider's
roots for a tighter trust store. Broad CA trust is acceptable here because **the
signature, not TLS, is the integrity boundary** — a TLS MITM still can't forge a
validly-signed manifest or image (it lacks your signing key); the worst it can do is
serve a stale signed update, which the anti-rollback floor blocks, or deny the download.

**The manifest + image.** `install()` consumes a signed manifest, which names the
reconstructed image's size/sha256 and the available **representations** and binds
`product_id`/`payload_version`/`min_platform_version` under one ECDSA signature (same keys as
the image). **One command** builds the whole publishable set from app source:
**`build ota-romfs`** — compiles + signs the bundle, renders `<board>-ota.img.gz`, signs
`<board>-manifest.bin`, and — with `--delta-from <factory-romfs.img>` — emits
`<board>-ota.delta.gz` + a delta representation. Host the artifacts beside each other;
representation URLs are **relative
filenames** (resolved against the manifest's URL on-device), so the signed manifest moves
between hosts without re-signing. (The device also accepts absolute `https://` URLs in a
manifest — what a dynamic update server emits when blobs live on a different origin than
the manifest endpoint — but the build CLI only ever writes relative ones.)

**Deltas.** A delta is a bsdiff-class patch against a **base image the device already
has** — under A/B, the slot it is currently running, which stays intact while the other is
written. The base is that slot's **body region only**, never its control sectors: those hold
per-device state (install counter, rollback entries, consumed attempt bytes, `CONFIRMED`), so
a patch allowed to copy from them would reconstruct differently on every device and fail the
sha256 gate. The installer refuses a patch that reads past the body region rather than
silently mixing device state into an image. The device reconstructs the new image from that base + the patch and only downloads
the changes, so a release that leaves the model blobs untouched (a config or key change)
ships as a few KB instead of the whole image. Because it carries a byte-difference stream,
even *scattered* small edits — a recompiled function, a table whose pointers all shifted —
fold into a cheap copy-with-difference rather than being re-sent. It's *opportunistic* — the
device picks the delta only when its running version matches the delta's base and the patch
is smaller, else the full image. A release may therefore ship **several** deltas, one per base
version still in the field — with only one, every device that has already updated falls back
to the full download, because under A/B the base is whatever release the device is running
(unlike v1, where every device kept the same golden forever). The delta is pure transport: the reconstructed slot is
still sha256- and signature-verified, so a bad patch simply never becomes the newest valid
slot. The applier ships in the romfs (it's OTA-patchable like the installer) and uses
`ulab` for the per-byte add — present on every OTA-capable board (it falls back to plain
Python where it isn't). Single-image devices never take a delta: their base is the slot
being erased.

## Bundled resources — applying romfs data to the device

`sync()` is a generic "take data bundled in the romfs and apply it to the device"
mechanism. It's driven by `app/lib/openmv_ota/data/`:

- **binary resources** live in `data/` (kept out of the `.py`), and
- **`data/resources.json`** is a manifest — a list of entries, each
  `{"file": …, "handler": …, …handler-specific args}`.

`sync()` is **handler-agnostic**: a resource's `handler` selects a `(matches, apply)`
pair, both called with `(entry, path)`. `matches` is the idempotence check ("already
applied?") and `apply` does the write; the entry carries whatever args that kind needs.
The loop has no per-resource assumptions:

```python
matches, apply = _HANDLERS[entry["handler"]]
if matches(entry, path):
    continue            # already up to date
apply(entry, path)
```

Today there is one handler, **`partition`**, used for the multi-core case: the helper
core's romfs is nested into the main image at `data/coprocessor.romfs` with the manifest
`{"file": "coprocessor.romfs", "handler": "partition", "partition": 1, …}`, and `sync()`
writes it into partition 1 when it differs (see
[Multi-core boards](02-projects.md#multi-core-boards-a-coprocessor-partition)). A future
kind — say writing keys or blowing fuses — is just another `(matches, apply)` pair
registered under a new `handler` name, plus its data file and manifest entry; `sync()`
itself doesn't change.

Two properties make this safe for sensitive resources (keys, fuses):

- **Authenticated by construction.** `data/` is part of the main ROMFS body, which the
  OTA trailer signs, and `sync()` only runs after `boot.py` verified and mounted that
  image. So a bundled resource is as trustworthy as the signed image it shipped in.
- **Verified + streamed.** Each `apply` reads its write back to confirm it took, and the
  `partition` handler streams the compare and the write a chunk at a time (and reads the
  erase back as all-`0xFF`), so even a ~1 MB image is never held in RAM whole.

## Debug logging

On-device OTA failures are otherwise invisible — `boot.py` runs before the REPL is up,
and `install()` reboots, so neither can `print()` anywhere you'll see. So there's an
opt-in logger built on the **standard `logging` module** (frozen on every OpenMV board
via the board manifest's `require("logging")`). `boot.py`, the installer, and the runtime
lib all log to the `openmv_ota` logger; your app uses the same standard tree:

```python
import logging
logging.getLogger("openmv_ota").info("hi")     # or: openmv_ota.log.info("hi")
```

The configuration lives in `device/openmv_log.py`, scaffolded into your project and frozen by
`build firmware` as **`openmv_log`** (frozen so `boot.py` can use it before `/rom` mounts).
It's **off by default** (the logger's level is set above `CRITICAL`, so nothing emits and
nothing leaks to the REPL). To debug on hardware, edit it and rebuild firmware:

```python
ENABLED = True         # master switch
UART    = 3            # your board's machine.UART id (the port differs per board)
BAUD    = 115200       # UART = None -> log to the USB REPL instead
LEVEL   = logging.INFO # show this level and above
```

Output is kernel-style. It prefers **wall-clock UTC from the RTC** — which is set by the
time the installer runs, because TLS cert validation requires it (`ntptime.settime()`) —
and falls back to **monotonic uptime** before the clock is set (e.g. in `boot.py`):

```
[   12.345] INFO openmv_ota: boot: mounted A (payload 1)                  (RTC unset)
[2026-06-25 12:34:56] WARNING openmv_ota: install: FAILED after erase     (RTC set)
```

`boot.py` logs the mounted slot and any reject reason; the installer logs each phase
(download / erase+write / done / failure); `confirm()`/`sync()` log their actions. Any
`machine.UART` is created once and kept by the handler. Because `device/openmv_log.py` is
*yours*, sending logs elsewhere (a file, a socket) is just editing its handler — the
levels, filtering, and API are the standard `logging` ones.

## Watchdog

A real app should run a watchdog so a hang reboots the device instead of bricking it.
Like the logger, there's an opt-in helper — `device/openmv_wdt.py`, frozen as
**`openmv_wdt`**, **off by default**, yours to edit. Turn it on and rebuild firmware:

```python
ENABLED    = True   # master switch (off by default — every openmv_wdt call is then a no-op)
WDT_ID     = None   # None = auto-select the DEEP-SLEEP-SAFE watchdog for this port
TIMEOUT_MS = 100    # reset if not fed within this long — MUST be ≤ the board's WDT max
TIMER_ID   = -1     # the soft timer (only id machine.Timer accepts; see relax() below)
FEED_HZ    = 50     # relax() ISR feed rate; keep well above 1000 / TIMEOUT_MS
```

Use the **deep-sleep-safe** watchdog — the one that *stops* while the device deep-sleeps, so it
can't reset you mid-sleep. `WDT_ID = None` auto-selects it per port: the **WWDG** on stm32/N6, the
default `machine.WDT` (WDOG / alif WDT) elsewhere. The catch is that the deep-sleep-safe watchdog is
**short** — the N6 WWDG maxes at 167 ms — so this is a **tens-of-milliseconds discipline**, not
seconds. (The always-counting IWDG can run for minutes but resets a *sleeping* device; pick it only
if your app never deep-sleeps.)

### The feed contract

Five rules. The **`main.py` that `openmv-ota project new --ota` scaffolds already follows all of
them** (it arms after camera setup, feeds per captured frame, and health-gates `confirm()`), and the
OTA install path is engineered to as well — that's what lets an update complete under an armed 100 ms
watchdog, proven on N6 + RT1060 hardware:

1. **Arm after setup, not at import.** Call `openmv_wdt.start()` once — when your slow one-time
   setup (camera reset, network bring-up) is *done* and you're entering the steady loop. Arming at
   import would let the ~100 ms window expire *during* that setup, before your first `feed()`, and
   reset the board. `start()` is a no-op while the watchdog is off, so leave it in unconditionally.
2. **Feed by real progress.** `openmv_wdt.feed()` once per loop of *actual work*, so a feed means
   work happened and a stuck loop stops feeding → reset. Don't feed from a bare timer just to keep
   it quiet — that masks the exact hang you wanted to catch.
3. **Feed on a tight cadence.** Every ~10–20 ms while awake (`await asyncio.sleep_ms(20)`), well
   under the window. A coarse `sleep(2)` loop *will* reset you.
4. **Split long ops, or `relax()` them.** One loop iteration must fit the window. If a step can't
   (a big model load), subdivide it and feed per step; only as a last resort wrap a truly
   unsplittable op in `with openmv_wdt.relax():` (see below).
5. **Boot needs no feeding.** `machine.reset()` — including the OTA trial reboot — clears the WWDG,
   so every boot runs unwatched until your app calls `start()` again. You never thread a feed
   through boot.py.

**Long blocking ops vs. the watchdog.** A multi-second flash erase (an OTA install), a
model load, etc. can't feed from the main loop and would trip the watchdog. Wrap them:

```python
with openmv_wdt.relax():
    do_long_thing()
```

`relax()` runs a `machine.Timer` whose callback feeds the watchdog at **interrupt time**,
so the board survives the op *as long as the CPU is healthy* (interrupts still firing) —
effectively suspending the watchdog without disabling it, and on exit it stops and hands
feeding back to your loop. Use it only around genuinely long ops; outside `relax()` the
watchdog still catches a hung loop. On every OpenMV port `machine.Timer` *is* the
virtual/soft timer (`-1`, the only id it accepts), and the helper creates it with
`hard=True` — that runs its callback in the SysTick/PendSV interrupt handler, which is
what lets the feed fire mid-erase. Without `hard=True` the callback is *scheduled* and
wouldn't run while the CPU is blocked, so the erase would still trip the watchdog.

**`install()` and `sync()` already do this, minimally** — each `relax()`es *only* the one
long flash erase (which it can't feed from a loop and which can exceed even the WDT's max
timeout) and `feed()`s the watchdog **per chunk** through the surrounding loops (`install`
through the download + write; `sync` through its write *and* the already-applied re-read).
So an OTA install or a `sync()` won't trip an enabled watchdog, yet a genuine stall
*isn't* masked: if a loop stops or a recv stalls, feeding stops and the watchdog resets the
board — which lands it back on the previous slot. `install()` also sets a 30 s socket timeout as the same backstop when no watchdog
is enabled (a stalled download fails cleanly instead of hanging). All a no-op if you
haven't enabled a watchdog.

## Safety properties at a glance

| Property | How |
|---|---|
| Never strand the device | `boot.py` boots the newest slot that *verifies*, so a bad update is simply not chosen — including a trial whose attempt it can't record. With no valid slot at all it hands off to firmware-resident recovery |
| Auto-rollback of a bad update | a trial that never `confirm()`s is rejected once its attempts run out, and the previous release runs instead. The attempt is recorded *before* the image runs, so a hang counts too |
| Writes can't fail silently | every on-device write is read back and verified; failures raise `OSError` |
| Bounded memory | slot bodies are `uctypes` views (no copy); SHA, resource compare, and the download/install all stream a chunk at a time |
| Trustworthy resources | bundled resources live in the signed ROMFS body and are applied only from a verified image |
| A proven fallback is never traded for an unproven one | an offered update is deferred while the running image is still an un-confirmed trial — enforced on the device, and mirrored on the server so the offer isn't wasted |
| The rollback floor can't regress | it is the max across both slots, and every install copies the current floor into the slot it writes |
| Safe install | `install()` writes the slot you are **not** running, downloads over verified HTTPS, read-back-verifies every write, and arms `pending` only after the whole image checks out; the image signature (not TLS) is the integrity boundary |

---

*[← 4 · Flashing](04-flashing.md) · [Index](00-introduction.md) · [6 · The update server →](06-update-server.md)*

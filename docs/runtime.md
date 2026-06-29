# The on-device OTA runtime

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
host-tested while the device I/O is exercised under QEMU — see [ci.md](ci.md).

## `boot.py` — slot selection at boot

An OTA partition is split into two slots: **FRONT** (the mutable, OTA-updated image)
and **BACK** (the immutable, factory-written *golden* image). On every boot `boot.py`
runs after the board's stock `_boot.py` and:

1. Reads FRONT's signed [trailer](trailer.md) and **verifies the ECDSA signature**
   (via the on-device mbedtls shim) *before trusting any header field*.
2. Checks the authenticated header: **integrity** (body SHA-256), **cross-flash guard**
   (`board_id`), **compatibility** (`min_platform_version`), and **anti-rollback**
   (`payload_version` vs the golden image's floor).
3. Runs the **trial state machine** (below) and mounts the chosen slot at `/rom`.

If FRONT fails *any* of these, `boot.py` falls back to the golden **BACK** image — so a
bad, corrupt, mis-targeted, downgraded, or un-confirmable update can never strand the
device. It records the outcome in module globals (`last_slot`,
`last_payload_version`, `last_failure_reason`) for the app to read, since the boot path
can't print.

## The update lifecycle (and your app's one job)

The trial mechanism is a one-shot commit. Markers in the slot's status sector —
`pending`, `tried`, `confirmed` — drive it:

```
updater stages a new FRONT image, sets `pending`
        │
   boot 1 ─ boot.py: pending only → arm `tried`, mount FRONT        (on trial)
        │
   your app runs, validates itself healthy → openmv_ota.confirm()   → `confirmed`
        │
   later boots: pending+tried+confirmed → mount FRONT               (committed)

   …but if the trial image hangs/crashes BEFORE confirm():
   boot 2 ─ boot.py: pending+tried+!confirmed → reject FRONT → mount golden BACK
```

So **your app must call `openmv_ota.confirm()` once it has proven itself healthy** —
otherwise the next boot treats the update as failed and rolls back. Confirm *after* a
real health check (sensors up, first frame, your self-test), **not** blindly at boot,
or you defeat the rollback safety.

One subtlety: if `boot.py` can't even *record* the trial (the `tried` write fails or
won't verify), it does **not** run the untracked FRONT — it falls back to golden. Better
to run the known-good image than an update we couldn't make recoverable.

## `openmv_ota` — the runtime library

`project new --ota` scaffolds `app/lib/openmv_ota/` (plain Python you own and can
extend); `build romfs` compiles + packs it to `/rom/lib/openmv_ota/`. It exposes:

- **`status()`** — read-only view of what boot.py did this boot (it mirrors its result
  onto `_ota_config`, the module the lib reads — importing boot.py would re-run it):
  - `slot` — `'FRONT'` | `'BACK'` | `None` (which image booted),
  - `fallback_reason` — why FRONT was rejected (`None` when on FRONT); `slot == 'BACK'`
    with a reason means **the last update failed and you're on the golden image** — worth
    reporting upstream,
  - `payload_version` — the booted image's version,
  - `pending` / `tried` / `confirmed` / `trial` — FRONT's trial-marker state.
- **`identity()`** — the running image's identity/provenance from `/rom/system.json`
  (`board`, `product`, `board_id`, `app_version`, `vendor`, toolchain, …) — what an
  update server reads to decide what to push. `{}` if there's no system.json.
- **`confirm()`** — keep the running image: writes `confirmed` **iff** you booted FRONT
  *and* it's an un-confirmed trial, else a no-op. Idempotent (safe to call every boot
  once healthy), returns whether it just confirmed. The FRONT-slot guard matters: if you
  fell back to BACK because a trial failed, FRONT still looks like an un-confirmed trial,
  so confirming it from BACK would resurrect the bad image — `confirm()` refuses to.
- **`sync()`** — apply any **bundled resources** (see below) whose on-device target
  differs from the bundled copy. A flash erase + chunked write of a whole partition, so
  **not quick** — it feeds the watchdog (`openmv_wdt`) the same minimal way `install()`
  does (`relax()` around the erase, `feed()` per chunk, including the already-applied
  re-read). Idempotent, returns the names applied; a no-op when nothing is bundled. Call
  it **early**, before a resource's consumer is used (e.g. before the helper core runs).
- **`install(url, ca=None)`** — download a gzipped FRONT-slot image over HTTPS and install
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
openmv_ota.confirm()              # keep this update (no-op unless a FRONT trial)

st = openmv_ota.status()
if st["fallback_reason"]:         # on golden because the last update failed -> report it
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
   — `board_id` cross-flash guard, `min_platform_version`, and the **anti-rollback floor**
   (the golden BACK image's version) — exactly mirroring what `boot.py` enforces on the
   image, just *earlier*. Any failure here raises with `/rom` intact.
3. **Selects a representation** from the manifest — the **full** image, or a **delta**
   when one is offered whose base matches this device's golden (BACK) version and it's
   smaller — and opens a second HTTPS GET for it.
4. Erases the FRONT slot, then **streams** the image straight in. For a full image:
   decompress a chunk → write → **read back and compare** → repeat, skipping erased `0xFF`
   runs. For a delta: stream-decompress the patch and reconstruct against the golden
   **BACK** slot (copy a run from BACK + add the patch's per-byte difference, vectorised
   with `ulab`; the patch is never held whole in RAM), writing+verifying the same way.
   Either way the stream is hashed and checked against the manifest's reconstructed-image
   **sha256** (fail-fast → golden). A ~1 MB image is never held in RAM. Handles
   `Content-Length`, chunked, close-delimited responses, and redirects.
5. Writes the `pending` marker **last**, only after the whole image verified, then
   reboots into the one-shot trial (your app then calls `confirm()` once healthy).

**It does not return on success — it reboots.** Two consequences:

- **Call it last.** The new image overwrites the FRONT slot the running app executes
  from, so once the erase starts the app can't continue. Bring the network up, do any
  teardown, *then* call `install()`. (The installer itself runs from RAM — `install()`
  reads `data/installer.py` and `exec`s it — so erasing the slot doesn't pull it out
  from under itself.)
- **Failure is safe.** A pre-flight failure (bad URL, DNS, TLS, HTTP status) raises
  **before** the erase, with `/rom` intact, so you can catch it and retry without a
  reboot. A failure *after* the erase reboots into the golden **BACK** image — boot.py
  rejects the half-written FRONT (bad signature/hash), and `status()` then reports the
  fallback so you know the update failed.

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
`board_id`/`payload_version`/`min_platform_version` under one ECDSA signature (same keys as
the image). **One command** builds the whole publishable set from app source:
**`build ota-romfs`** — compiles + signs the bundle, renders `<board>-ota.img.gz`, signs
`<board>-manifest.bin`, and — with `--delta-from <factory-romfs.img>` — emits
`<board>-ota.delta.gz` + a delta representation. Host the artifacts beside each other;
representation URLs are **relative
filenames** (resolved against the manifest's URL on-device), so the signed manifest moves
between hosts without re-signing. (The device also accepts absolute `https://` URLs in a
manifest — what a dynamic update server emits when blobs live on a different origin than
the manifest endpoint — but the build CLI only ever writes relative ones.)

**Deltas.** A delta is a bsdiff-class patch against the **golden** (the immutable BACK
slot every device keeps): the device reconstructs the new image from BACK + the patch and
only downloads the changes, so a release that leaves the model blobs untouched (a config
or key change) ships as a few KB instead of the whole image. Because it carries a
byte-difference stream, even *scattered* small edits — a recompiled function, a table whose
pointers all shifted — fold into a cheap copy-with-difference rather than being re-sent.
It's *opportunistic* — the device picks the delta only when its golden matches the delta's
base and it's smaller, else the full image. The delta is pure transport: the reconstructed
slot is still sha256- and signature-verified, so a bad patch just falls back to golden. The
applier ships in the romfs (it's OTA-patchable like the installer) and uses `ulab` for the
per-byte add — present on every OTA-capable board (it falls back to plain Python where it
isn't). One `golden → latest` delta updates any device, whatever version it's currently
running — there are no per-version delta chains.

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
[Multi-core boards](project.md#multi-core-boards-a-coprocessor-partition)). A future
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
[   12.345] INFO openmv_ota: boot: mounted FRONT (payload 1)              (RTC unset)
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
**`openmv_wdt`**, off by default, yours to edit. Enable it and pick your board's
`machine.WDT` id + timeout, then rebuild:

```python
ENABLED    = True
WDT_ID     = 0
TIMEOUT_MS = 5000
TIMER_ID   = -1    # the soft timer (only id machine.Timer accepts; see below)
FEED_HZ    = 10
```

Feed it from your main loop — if the loop ever stops, the board resets:

```python
import openmv_wdt
while True:
    openmv_wdt.feed()
    ...
```

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
*isn't* masked: if a loop stops or a recv stalls, feeding stops and the watchdog resets to
golden. `install()` also sets a 30 s socket timeout as the same backstop when no watchdog
is enabled (a stalled download fails cleanly instead of hanging). All a no-op if you
haven't enabled a watchdog.

## Safety properties at a glance

| Property | How |
|---|---|
| Never strand the device | `boot.py` falls back to the golden BACK image on any FRONT failure, including a trial it can't record |
| Auto-rollback of a bad update | one-shot trial: an image that never `confirm()`s is rejected on the next boot |
| Writes can't fail silently | every on-device write is read back and verified; failures raise `OSError` |
| Bounded memory | slot bodies are `uctypes` views (no copy); SHA, resource compare, and the download/install all stream a chunk at a time |
| Trustworthy resources | bundled resources live in the signed ROMFS body and are applied only from a verified image |
| Safe install | `install()` downloads over verified HTTPS, read-back-verifies every write, arms `pending` only after the whole image checks out, and reboots into golden BACK on any post-erase failure; the image signature (not TLS) is the integrity boundary |

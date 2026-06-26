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
  differs from the bundled copy. Idempotent, returns the names applied; a no-op when
  nothing is bundled. Call it **early**, before a resource's consumer is used (e.g.
  before the helper core runs).

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

## Safety properties at a glance

| Property | How |
|---|---|
| Never strand the device | `boot.py` falls back to the golden BACK image on any FRONT failure, including a trial it can't record |
| Auto-rollback of a bad update | one-shot trial: an image that never `confirm()`s is rejected on the next boot |
| Writes can't fail silently | every on-device write is read back and verified; failures raise `OSError` |
| Bounded memory | slot bodies are `uctypes` views (no copy); SHA, resource compare, and resource write all stream a chunk at a time |
| Trustworthy resources | bundled resources live in the signed ROMFS body and are applied only from a verified image |

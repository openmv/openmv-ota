# The device library

*[← 11 · Boot and rollback](11-boot-and-rollback.md) · [Index](00-introduction.md) · [13 · Logging & the watchdog →](13-logging-and-watchdog.md)*

---

[Boot and rollback](11-boot-and-rollback.md) covered the half that chooses what runs;
**`openmv_ota`** is the half your app calls. `project new --ota` scaffolds it into
`app/lib/openmv_ota/` (plain Python you own and can extend); `build romfs` compiles +
packs it to `/rom/lib/openmv_ota/`. It exposes:

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
    (`tried` is informational only — the attempt region is what `boot.py` counts.)
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
- **`sync()`** — apply any **bundled resources** whose on-device target
  differs from the bundled copy. A flash erase + chunked write of a whole partition, so
  **not quick** — it feeds the watchdog (`openmv_wdt`) the same minimal way `install()`
  does (`relax()` around the erase, `feed()` per chunk, including the already-applied
  re-read). Idempotent, returns the names applied; a no-op when nothing is bundled. Call
  it **early**, before a resource's consumer is used (e.g. before the helper core runs).
- **`install(url, ca=None)`** — fetch a gzipped slot image over HTTPS — or from a
  file path — and install it. Does **not** return
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
`url` is the **signed manifest** (`build ota-romfs`), *not* a raw image — the device
resolves the actual image from the manifest itself (representation URLs are relative to
the manifest's location by default). It is an `https://` URL, **or a file path**: copy
the published artifacts onto a mounted filesystem (an SD card, realistically) and
`install("/sdcard/fw/OPENMV_N6-manifest.bin")` installs with no network at all — through the
identical signature, vetting, and anti-rollback checks, because the medium is untrusted
either way and the signature is the boundary. Something else decides *which* manifest to
hand it (how that's obtained is out of scope here). It:

1. Opens an **HTTPS** connection (plaintext HTTP is refused), verifying the server
   against `ca` with `CERT_REQUIRED` + SNI — all **before** erasing anything. A file
   install opens the manifest file instead: no connection, and `ca` is ignored.
2. **Fetches + verifies the manifest** (into RAM): checks its ECDSA signature against the
   same frozen trusted keys as an image trailer, then applies the device-relative checks
   — `product_id` cross-flash guard, `min_platform_version`, and the **anti-rollback floor**
   (the highest version any slot has recorded) — exactly mirroring what `boot.py` enforces
   on the image, just *earlier*. Any failure here raises with `/rom` intact.
3. **Selects a representation** from the manifest — the **full** image, or a **delta**
   when one is offered whose base matches the version this device is *running* and it's
   smaller — and opens a second HTTPS GET (or the sibling file) for it. (Single-image devices never take a delta:
   the base would be the very slot about to be erased.)
4. **Picks its target slot — the one the device is not running** — and erases it, then
   **streams** the image straight in. For a full image: decompress a chunk → write → **read
   back and compare** → repeat, skipping erased `0xFF` runs. For a delta: stream-decompress
   the patch and reconstruct against the **running** slot (copy a run from it + add the
   patch's per-byte difference; the patch is never held whole in RAM),
   writing+verifying the same way. Either way the stream is hashed and checked against
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
- **Failure is safe.** A pre-flight failure (bad URL or path, DNS, TLS, HTTP status) raises
  **before** the erase, with `/rom` intact, so you can catch it and retry without a reboot.
  A failure *after* the erase reboots, and boot.py rejects the half-written slot (bad
  signature/hash) and mounts the previous release — the last update that worked; `status()`
  then reports the fallback so you know the update failed.

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

**TLS trust.** The `ca` argument selects the PEM trust store:

| `ca` | Trust store |
| --- | --- |
| `None` (default) | the bundled `data/ca.pem` |
| a `str` | a file path to read |
| `bytes` | used directly |

`project new` downloads a fresh Mozilla root bundle into `data/ca.pem`, so common
public CAs verify out of the box; replace it with your own provider's roots for a
tighter trust store. Broad CA trust is acceptable here because **the signature,
not TLS, is the integrity boundary** — a TLS MITM still can't forge a
validly-signed manifest or image (it lacks your signing key); the worst it can do
is serve a stale signed update, which the anti-rollback floor blocks, or deny the
download.

**The manifest + image.** `install()` consumes the **signed manifest** that
[`build ota-romfs`](08-release-artifacts.md#build-ota-romfs) produces beside the
image and any deltas: it names the image's size/sha256 and the available
**representations**, and binds `product_id`/`payload_version`/
`min_platform_version` under the same ECDSA key as the image. Host the artifacts
beside each other; representation URLs are **relative filenames**, resolved
on-device against the manifest's own URL, so the signed manifest moves between
hosts without re-signing. (The device also accepts absolute `https://` URLs in
a manifest; everything this tool produces is relative.)

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
to the full download, because under A/B the base is whatever release the device is running.
The delta is pure transport: the reconstructed slot is
still sha256- and signature-verified, so a bad patch simply never becomes the newest valid
slot. The applier ships in the romfs, so it is OTA-patchable like the
installer. Single-image devices never take a delta: their base is the slot
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
[Multi-core boards](04-ota-projects.md#multi-core-boards-a-coprocessor-partition)). A future
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
| Never strand the device | `boot.py` boots the newest slot that *verifies*, so a bad update is simply not chosen — including a trial whose attempt it can't record. With no valid slot at all it hands off to firmware-resident recovery |
| Auto-rollback of a bad update | a trial that never `confirm()`s is rejected once its attempts run out, and the previous release runs instead. The attempt is recorded *before* the image runs, so a hang counts too |
| Writes can't fail silently | every on-device write is read back and verified; failures raise `OSError` |
| Bounded memory | slot bodies are `uctypes` views (no copy); SHA, resource compare, and the download/install all stream a chunk at a time |
| Trustworthy resources | bundled resources live in the signed ROMFS body and are applied only from a verified image |
| A proven fallback is never traded for an unproven one | an offered update is deferred while the running image is still an un-confirmed trial — enforced on the device, and mirrored on the server so the offer isn't wasted |
| The rollback floor can't regress | it is the max across both slots, and every install copies the current floor into the slot it writes |
| Safe install | `install()` writes the slot you are **not** running, downloads over verified HTTPS, read-back-verifies every write, and arms `pending` only after the whole image checks out; the image signature (not TLS) is the integrity boundary |

---

*[← 11 · Boot and rollback](11-boot-and-rollback.md) · [Index](00-introduction.md) · [13 · Logging & the watchdog →](13-logging-and-watchdog.md)*

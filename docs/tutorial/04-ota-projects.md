# OTA projects

*[← 3 · The lock](03-the-lock.md) · [Index](00-introduction.md) · [5 · Signing keys →](05-signing-keys.md)*

---

By default a project builds a single image that fills the whole ROMFS partition.
Pass `--ota` at `project new` to declare an **over-the-air** project instead — one whose
images a camera can download, verify, and fall back from.

## What `--ota` changes

- **Partition split.** Each partition is split into the two **slots** from the
  introduction — A and B — so each image gets half the partition, less the slot's
  two 4 KiB control sectors (the trial status — which also carries the
  anti-rollback floor in its tail — and the signed trailer: 8 KiB).
  `build romfs` enforces that halved budget for an OTA project and the full
  partition otherwise; `show` reports which mode a project is in. The mode is
  recorded as `[ota] enabled` in `openmv-ota.toml` and mirrored into the lock;
  changing it re-resolves the project, so set it at `new` time.

  Not every board can host two slots. A board whose ROMFS lives in a single large
  internal-flash sector — OpenMV2/3/4, where the erase block *is* the whole
  partition — builds in **single-image mode** instead: one slot spanning the
  partition, no on-flash fallback (a failed update there is re-downloaded by
  firmware-resident recovery).
  Two-slot boards keep their ROMFS in external NOR/OSPI flash (4 KiB erase
  blocks) or MRAM.

- **A trust store the firmware can carry.** Recovery needs TLS anchors in the
  firmware itself. On the OpenMV N6, AE3, and RT1062 the firmware is large
  enough to hold the full public bundle, so nothing needs configuring. On every
  other board it is not — there `new --ota` requires an explicit `--ca` root
  (your server's own root, a few KB) and refuses without one.

- **Keys provisioned.** `new --ota` generates the product's whole signing key
  set up front and writes it under `keys/`.

- **The product id is enforced.** Every build stamps the scaffolded `product_id`
  ([page 2](02-projects.md#product-name-vs-board-name)) into the image; an OTA
  device bakes its own copy in and rejects any image whose id doesn't match — the
  **cross-flash guard**. Keep the id once devices ship (a later change would
  reject updates on fielded devices); `build romfs` warns if it is `0` (guard
  off) or if two boards collide on one id.

(The starter `app/` — including the `app_version` the build stamps into the image
— is scaffolded for every project, not just OTA; see
[The app folder](02-projects.md#the-app-folder).)

### The version and the rollback floor

The build reads `app_version` from `app/settings.json` and stamps it into the
image, making that file the one place a version is defined:

```json
{
  "app_version": "1.0.0",
  "vendor": "Acme Robotics"
}
```

Anti-rollback needs no configuration. Every device keeps a **rollback floor** —
the highest version it has ever *kept* — and refuses anything older than it: the
installer rejects an offered update below the floor before erasing anything, and
at boot an *unproven* image below the floor is rejected too. The floor starts at
the factory image's version and rises by itself each time your app calls
`confirm()`, so a replayed old release — even a validly signed one — cannot come
back.

How the floor survives is the one place the two modes differ:

- **A/B.** Either slot can be erased by the next install, so the floor is
  recorded in *both* slots and the device takes the highest; the installer
  copies the current floor into every slot it writes. Your own fallback is never
  locked out: a slot this device already ran and kept stays bootable after the
  floor rises past it — the floor gates what may be *installed* and what may run
  *unproven*, not the proven release behind you.
- **Single-image.** Same records, one slot: the installer carries the floor into
  the slot it rewrites, which is what lets the floor survive the erase of the
  only slot that held it.

### OTA options at `new`

| Flag | Effect |
|---|---|
| `--ota` | Declare the project over-the-air: split each partition into slots and provision the signing keys. |
| `--ca PEM` | TLS roots the device trusts for OTA downloads, copied into the project and frozen into the firmware. Unset fetches the public Mozilla bundle — allowed only on boards whose firmware can carry it (N6, AE3, RT1062). |
| `--no-firmware-patches` | Don't auto-apply the OTA-required firmware patches; fail instead if the firmware lacks them. |

## Files an OTA project adds

On top of the files a plain project writes (settings, `.gitignore`, `README.md`,
and the starter `app/`), `new --ota` creates the keys and extends the config:

```
my-product/
├── openmv-ota.toml          # gains an [ota] section
├── app/lib/openmv_ota/      # the device OTA runtime library (status/confirm/sync/install)
│   └── data/
│       └── installer.py     # the installer, shipped as source (exec'd into RAM)
├── certs/
│   └── ca.pem               # TLS trust store, frozen into the firmware by `build
│                            # firmware` (fetched fresh at `new`; `--ca` copies here)
├── device/
│   ├── openmv_log.py               # the OTA debug logger
│   └── openmv_wdt.py               # the watchdog helper
└── keys/
    ├── trusted_keys.json    # committed: the public key set baked into firmware
    └── private/             # GITIGNORED: the private signing keys (PKCS#8 PEM)
        ├── factory-0001.pem … factory-0008.pem
        └── ota-0100.pem     … ota-011f.pem
```

The generated `.gitignore` already excludes `keys/private/` (and `keys/*.pem`,
`keys/*.key`). **Commit `keys/trusted_keys.json`; never commit `keys/private/`** —
only the signing machine holds the private keys, and there is no recovery if they
leak (an attacker could sign images your devices would trust) or are lost (you can
rotate to another provisioned key, but a key never provisioned can't be added).
Back the private keys up out-of-band.

The `[ota]` section records the mode and the current signing key:

```toml
[ota]
enabled = true            # each partition holds two updatable slots (A/B)
signing_key_id = 256      # current OTA signing key (in keys/trusted_keys.json)
max_attempts = 3          # boots a trial gets to confirm (optional; frozen into the firmware)
```

## The device runtime library (`openmv_ota`)

`new --ota` also scaffolds `app/lib/openmv_ota/` — the device-side OTA helpers your app
imports on the camera (`build romfs` compiles + packs them to `/rom/lib`). The short
version: call **`confirm()`** once your app is healthy (so a new update is kept rather
than rolled back on the next boot), **`sync()`** early (to apply any resources
bundled in the image), and **`status()`** to inspect the trial state:

```python
import openmv_ota
openmv_ota.sync()        # apply any resources bundled in this image
# ... once your app has validated itself healthy:
openmv_ota.confirm()     # keep the update (no-op if it isn't a trial)
```

It also exposes **`install(url)`** — fetch a new image over HTTPS, or from a file
path, and install it.
The installer ships as source in `data/installer.py` (so the device can `exec` it into
RAM while it overwrites the slot it runs from), and `certs/ca.pem` is the TLS trust
store: **`new --ota` downloads a fresh Mozilla root bundle into it** (this step needs
network, like the SDK download) and `build firmware` freezes it, so the device reads
its anchors straight out of flash. You can replace it with your provider's roots.
What `install()` downloads is produced by `build ota-romfs`.

`new --ota` also scaffolds two **opt-in** helpers under `device/`, each frozen into
the firmware by `build firmware` (as `openmv_log` / `openmv_wdt`) and off by
default. **`openmv_log.py`** is a debug logger built on the standard `logging`
module, shared by `boot.py`, the installer, and this lib, and exposed to your app
as `openmv_ota.log`; edit it to enable + pick your board's UART, then rebuild
firmware. **`openmv_wdt.py`** is a watchdog helper — `openmv_wdt.feed()` from your
main loop, `with openmv_wdt.relax():` around long blocking ops (a timer ISR feeds
through them) — and `install()` uses it automatically.

## Multi-core boards (a coprocessor partition)

On a multi-core board ([page 2](02-projects.md#multi-core-boards)) only the
**main** partition is OTA. It is built from `app/`, OTA-wrapped like any other;
the coprocessor partition stays a *plain* romfs — the helper core has no mbedtls
and can't verify signatures, so it is never updated on its own.

**Writing the helper partition at runtime.** For an OTA project, `build romfs` also
**nests** the coprocessor image inside the main one, at
`/rom/lib/openmv_ota/data/coprocessor.romfs`, with a `resources.json` manifest. So an
OTA update to the main carries the matching helper image, and your app calls
[`openmv_ota.sync()`](#the-device-runtime-library-openmv_ota) early in `main.py` to
write it into the helper partition (only when it differs). Because the nested image
travels *with* the main, `sync()` always writes the helper image that matches the main
that's actually running — so it stays consistent even across a rollback to the previous
image. (The standalone `-coprocessor-romfs.img` is for flashing the helper partition
directly at the factory; the nested copy is byte-identical.)

---

*[← 3 · The lock](03-the-lock.md) · [Index](00-introduction.md) · [5 · Signing keys →](05-signing-keys.md)*

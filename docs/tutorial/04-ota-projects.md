# OTA projects

*[← 3 · The lock](03-the-lock.md) · [Index](00-introduction.md) · [5 · Signing keys →](05-signing-keys.md)*

---

By default a project builds a single image that fills the whole ROMFS partition.
Pass `--ota` at `project new` to declare an **over-the-air** project instead — one whose
images a camera can download, verify, and fall back from.

## What `--ota` changes

- **Partition split.** Each partition is split into the two **slots** from the
  introduction — A and B — so each image gets half the partition, less the slot's
  four 4 KiB control sectors (a spare, the rollback log, the trial status, and the
  signed trailer — 16 KiB).
  `build romfs` enforces that halved budget for an OTA project and the full
  partition otherwise; `show` reports which mode a project is in. The mode is
  recorded as `[ota] enabled` in `openmv-ota.toml` and mirrored into the lock;
  changing it re-resolves the project, so set it at `new` time.

  Not every board can host two slots. A board whose ROMFS lives in a single large
  internal-flash sector — OpenMV2/3/4, where the erase block *is* the whole
  partition — builds in **single-image mode** instead: one slot spanning the
  partition, no on-flash fallback (a failed update there is re-downloaded by
  firmware-resident recovery). One practical
  consequence: the public TLS bundle does not fit their small slot, so on these
  boards `new --ota` requires an explicit `--ca` root and refuses without one.
  Two-slot boards keep their ROMFS in external NOR/OSPI flash (4 KiB erase
  blocks) or MRAM.

- **Keys provisioned.** A device trusts exactly the public keys baked into its
  firmware, and you cannot add a trusted key later without re-flashing. So `new
  --ota` provisions the *whole* key set up front and
  writes it under `keys/`.

- **Identity pinned.** The per-board `product_id` / `board_name` (from the
  [projects page](02-projects.md#product-name-vs-board-name)) are now *written into
  the config* as active `[targets.<BOARD>]` tables, so the derived id is frozen —
  fielded devices bake it in.

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
| `--ca PEM` | TLS roots the device trusts for OTA downloads, copied into the project and frozen into the firmware. Unset fetches the public Mozilla bundle. |
| `--no-firmware-patches` | Don't auto-apply the OTA-required firmware patches; fail instead if the firmware lacks them. |

## Files an OTA project adds

On top of the files a plain project writes (settings, `.gitignore`, `README.md`,
and the starter `app/`), `new --ota` creates the keys and extends the config:

```
my-product/
├── openmv-ota.toml          # gains an [ota] section + per-board [targets.*] tables
├── app/lib/openmv_ota/      # the device OTA runtime library (status/confirm/sync/install)
│   └── data/
│       ├── installer.py     # the installer, shipped as source (exec'd into RAM)
│       └── ca.pem           # TLS root bundle for downloads (fetched fresh at `new`)
├── device/
│   ├── openmv_log.py               # OTA debug logger, frozen as openmv_log (off by default)
│   └── openmv_wdt.py               # watchdog helper, frozen as openmv_wdt (off by default)
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
```

and each target board gets an active table for its identity:

```toml
[targets.OPENMV_N6]
product_id   = 396486252   # stable product id (auto-assigned; keep it once devices ship)
board_name = "my-product"  # human label; defaults to the product name, rename freely
```

## The device runtime library (`openmv_ota`)

`new --ota` also scaffolds `app/lib/openmv_ota/` — the device-side OTA helpers your app
imports on the camera (`build romfs` compiles + packs them to `/rom/lib`). The short
version: call **`confirm()`** once your app is healthy (so a new update is kept rather
than rolled back on the next boot), **`sync()`** early (to apply bundled resources like
a helper core's romfs), and **`status()`** to inspect the trial state:

```python
import openmv_ota
openmv_ota.sync()        # bring bundled resources up to date with this image
# ... once your app has validated itself healthy:
openmv_ota.confirm()     # keep the update (no-op if it isn't a trial)
```

It also exposes **`install(url)`** — download a new image over HTTPS and install it.
The installer ships as source in `data/installer.py` (so the device can `exec` it into
RAM while it overwrites the slot it runs from), and `data/ca.pem` is the TLS trust
store: **`new --ota` downloads a fresh Mozilla root bundle into it** (this step needs
network, like the SDK download), and you can replace it with your provider's roots.
What `install()` downloads is produced by `build ota-romfs`.

For debugging on hardware, `new --ota` also scaffolds **`device/openmv_log.py`** — an opt-in
logger built on the standard `logging` module (frozen as `openmv_log`, off by default)
shared by `boot.py`, the installer, and this lib, and exposed as `openmv_ota.log` (the
`logging.getLogger("openmv_ota")` logger) for your app. Edit it to enable + pick your
board's UART, then rebuild firmware.

It also scaffolds **`device/openmv_wdt.py`** — an opt-in watchdog helper (frozen as
`openmv_wdt`, off by default): `openmv_wdt.feed()` from your main loop, and
`with openmv_wdt.relax():` around long blocking ops (a timer ISR feeds the watchdog
through them). `install()` uses it automatically.

## Board identity

`product_id` is a `uint32` that names a product (the cross-flash guard), and
`board_name` is a human label for it. They live only in `openmv-ota.toml` (per
`[targets.<BOARD>]`) and are pure identity — **excluded from the lock and its
[`config_digest`](03-the-lock.md#what-the-lock-records)** — so setting a product id or renaming a board never trips drift
(unlike geometry overrides, which are firmware-relevant and *are* digested).
`build romfs` reads them and stamps them into `system.json` and the trailer: the
device's `product_id` guards against cross-flashing the wrong product; `board_name`
is metadata only.

The number is the same derivation the
[projects page](02-projects.md#product-name-vs-board-name) describes — `new --ota`
just writes it into the config so it's *frozen*. **Keep it once devices ship**: a
device bakes its `product_id` in and rejects any image whose id doesn't match, so a
later change would reject updates on fielded devices. You can still override it (e.g. to match an existing product numbering),
and `build romfs` warns if you set it to `0` (guard off) or if two boards collide
on the same id.

A **non-OTA** project doesn't pin a `product_id` in its config (the guard only
applies to OTA), but `build romfs` still derives the same stable id and records it
in `system.json`, so a non-OTA app reads the same product identity — and nothing
changes when you later move to OTA. One app folder can target several boards or
products at once — each gets its own identity but shares the app and toolchain.

## Multi-core boards (a coprocessor partition)

Some boards have a second core with its own ROMFS partition. The AE3 is dual-core:
the **main** high-performance core (partition 0, OSPI, 24 MiB) runs OTA, and a
**coprocessor** high-efficiency core (partition 1, MRAM, 1 MiB) is *slaved* to it —
it's booted by the main core, and its romfs is written by the main core. Each
partition carries a **role** (`main` or `coprocessor`).

There is nothing to configure: the coprocessor is slaved, so the tool **always
builds every partition automatically**. You don't list partitions and there's no
`--partition` flag. The main partition is built from `app/` (OTA-wrapped in an OTA
project); the coprocessor partition is built from a second folder, **`app-coprocessor/`**,
as a *plain* romfs (never OTA — the helper core has no mbedtls and can't verify
signatures). `project new` scaffolds `app-coprocessor/` automatically when a selected
board has a coprocessor partition.

Outputs are named by role: the main partition keeps the bare board name
(`OPENMV_AE3-romfs.img` / `-factory-romfs.img`), and the coprocessor partition is
suffixed (`OPENMV_AE3-coprocessor-romfs.img`). The coprocessor image is the same
plain romfs from both `build romfs` and `build factory-romfs` — it's the image the
main core writes into the helper's slot.

Each partition is resolved independently — its own size, alignment rules, NPU config,
and role — and appears as its own resolved target in the lock. From Python,
select one with `board(name, partition)`, or iterate `targets`.

> A `partition_size` override (under `[targets.<board>]`) applies only to the main
> partition; the coprocessor always keeps its firmware geometry.

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

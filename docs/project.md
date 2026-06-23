# project

`openmv-ota project` pegs an OTA project to a specific OpenMV firmware checkout
and records the toolchain versions and per-board geometry that firmware implies.
Model compilers (mpy-cross, Ethos-U Vela, ST Edge AI) must match the libraries
built into the firmware, so a project captures the exact versions to use.

A project is a directory you commit to git. It holds three files:

- `openmv-ota.toml` — the config you edit: product metadata and target boards.
- `openmv-ota.lock.json` — the resolved snapshot: the firmware's git remote and
  commit, submodule commits, firmware / MicroPython / SDK / tool versions, and
  per-board geometry. Committed, and contains no machine paths.
- `openmv-ota.local.toml` — this machine's firmware checkout path. Gitignored.

The firmware checkout itself is referenced by path, not copied into the project.

## Layout

The project directory holds the settings alongside your MicroPython app:

```
my-product/
├── openmv-ota.toml          # committed: product metadata + target boards
├── openmv-ota.lock.json     # committed: the pegged snapshot
├── openmv-ota.local.toml    # gitignored: this machine's firmware checkout path
├── .gitignore
├── README.md
├── app/                     # your MicroPython app: main.py, settings.json, lib/, models, …
├── keys/                    # OTA only: trusted_keys.json (committed) + private/ (gitignored)
└── build/                   # gitignored: build output (one .romfs per target)
```

`new` writes the settings files, `.gitignore`, `README.md`, and a starter `app/`
— a placeholder `main.py` and a `settings.json` carrying your app version (see
[The app folder](#the-app-folder)). Replace `main.py` with your code; an OTA
project (`--ota`) additionally provisions `keys/` (see
[OTA projects](#ota-projects)). `openmv-ota build romfs` compiles the app and
writes images to `build/`. Commit everything except `openmv-ota.local.toml`,
`keys/private/`, and `build/`, which the generated `.gitignore` already excludes.
(`app/` and `build/` are the defaults; `build romfs` takes `--app` and `--output`
to use other directories.)

## Creating a project

`new` pegs a project to a local OpenMV checkout:

```bash
openmv-ota project new ./my-product -f ~/openmv -b OPENMV_N6 -b OPENMV_AE3
```

It reads the checkout and the installed SDK, writes the three files, and a
`.gitignore`. Commit `openmv-ota.toml` and `openmv-ota.lock.json`; the
`.gitignore` keeps `openmv-ota.local.toml` out of the repository.

`new` expects the OpenMV SDK to be installed already (run `make sdk` in the
firmware checkout). Pass `--install-sdk` to run `make sdk` for you, or
`--sdk-home PATH` to point at an SDK in a non-default location.

### Options

| Flag | Effect |
|---|---|
| `-f, --firmware PATH` | The local OpenMV checkout to peg to (required). |
| `-b, --board NAME` | A target board (repeatable, at least one). |
| `--product NAME` | Product name (defaults to the directory name). |
| `--vendor NAME` | Vendor name. |
| `--sdk-home PATH` | SDK install directory (default `~/openmv-sdk-<version>`). |
| `--install-sdk` | Run `make sdk` if the SDK is missing. |
| `--allow-dirty` | Don't warn when the checkout has uncommitted changes. |
| `--ota` | Over-the-air project: split each partition and provision signing keys. |
| `--sig-alg {ES256,ES384,ES512}` | OTA signature algorithm (default `ES256` / P-256). |
| `--ota-keys N` | OTA rotation-pool size to provision (default 32). |
| `--factory-keys N` | Factory-key reserve to provision, one per manufacturing site (default 8). |
| `--force` | Overwrite an existing project. |

## The app folder

Every project — OTA or not — is scaffolded with a starter `app/`:

```
app/
├── main.py        # placeholder; replace with your code
├── settings.json  # your app's version and settings
└── lib/           # your own importable modules (kept in git by a .gitkeep)
```

`lib/` is the place for the app's own library modules — the code `main.py`
imports, factored out of it. It starts empty (a `.gitkeep` keeps the directory in
git); the `.gitkeep` is never packed into an image. Add `lib/helpers.py` and
`main.py` can `import helpers`.

`settings.json` is the single, user-editable home for your app's version and any
settings you want to read at runtime:

```json
{
  "app_version": "1.0.0",
  "vendor": "",
  "rollback_floor": "1.0.0"
}
```

It is packed into the ROMFS image, so the app can read it on-device (e.g.
`json.load(open("/rom/settings.json"))`) — useful in any project for reporting a
version or carrying configuration. Bump `app_version` (a `major.minor.patch`
semver) for each release. For an **OTA project**, the build also reads
`app_version` from here to stamp the image's anti-rollback version (see
[build.md](build.md)), making this file the one place a version is defined.

`rollback_floor` is the **oldest app version you will ever allow back onto a
device**. The build records it in the OTA image, and the updater refuses to install
anything below it. It starts equal to your first `app_version`, so it constrains
nothing yet (nothing is older than your first release). **It is not a per-release
version — leave it alone for normal releases.** Raise it *only* when a release
fixes something that must never be bypassed by a downgrade (a security patch, say);
once raised, devices permanently refuse any image below that floor — **including
your own rollbacks** — so move it deliberately. It must stay `<= app_version`
(an image can't violate its own floor), and the build fails if it doesn't.

`new` only writes `main.py` and `settings.json` if they are absent, so re-running
`new --force` never clobbers your app.

### `system.json` (generated, read-only)

Keep *user-editable* settings in `settings.json`. *Derived* values — board
identity and build provenance — must not be hand-edited, so the build generates a
separate, read-only **`system.json`** into every image (OTA or not) at
`/rom/system.json`:

```json
{
  "product": "orchard-sentry",
  "board": "OPENMV_N6",
  "board_id": 4097,
  "board_name": "OrchardSentry Pro",
  "app_version": "2.3.0",
  "vendor": "Acme Robotics",
  "ota": true,
  "firmware": {"version": "5.0.0", "commit": "9f2c1ab3d4e5f60718293a4b5c6d7e8f90a1b2c3"},
  "micropython": "1.28.0",
  "toolchain": {"mpy_cross": "1.28.0", "vela": "3.12.0", "stedgeai": "2.1.0", "sdk": "1.6.0"}
}
```

This gives the app **one consistent read path for system state, the same in a
non-OTA and an OTA build** — `json.load(open("/rom/system.json"))`. It is composed
from the lock (firmware / MicroPython / toolchain provenance) and the config
(per-board `board_id` / `board_name`); for an OTA image the signed
[trailer](trailer.md) also carries a verbatim copy, so host tools can read it
without mounting the ROMFS. `system.json` is generated into the built image only —
never into your `app/` source — so there is nothing to edit or accidentally commit.
(The name is reserved; a `system.json` in your `app/` is overwritten.)

### Product name vs board name

`system.json` carries three identity fields, and how `product` and `board_name`
relate depends on how many boards a project targets:

- **`board`** is always the canonical board key (`OPENMV_N6`) — the technical
  target, never renamed.
- **`product`** is your project/product name (`[product].name`, defaulting to the
  directory name). It is the same for every board the project builds.
- **`board_name`** is a human label, set per board under `[targets.<BOARD>]`. If
  you don't set it, it **defaults to `product`**.

For a **single-board project**, `product` and `board_name` are the same string by
default — you can ignore `board_name` and read `product`. For a **multi-board
project** (one app folder targeting several boards), `product` stays constant
while you can give each board its own `board_name` to distinguish the variants in
the field — e.g. one app built for two boards sold as "My Product Lite" and "My
Product Pro":

```toml
[product]
name = "my-product"          # product, shared by every board

[targets]
boards = ["OPENMV_N6", "OPENMV_AE3"]

[targets.OPENMV_N6]
board_id   = 1001
board_name = "My Product Lite"

[targets.OPENMV_AE3]
board_id   = 1002
board_name = "My Product Pro"
```

Set `board_name` only when you ship the one product on multiple boards and want
them named apart; otherwise leave it and `product` carries the name.

## OTA projects

By default a project builds a single image that fills the whole ROMFS partition.
Pass `--ota` to declare an over-the-air project instead. `--ota` does three things
beyond a plain project — it splits the partition and provisions the signing keys —
so that, with the app version already in [the app folder](#the-app-folder), an OTA
project is ready to build a signed image with one command.

### What `--ota` changes

- **Partition split.** Each partition is split into two halves — a regular image
  and a golden fallback — so each image gets half the partition, less an 8 KiB
  status sector and trailer. `build romfs` enforces that halved budget for an OTA
  project and the full partition otherwise; `show` reports which mode a project is
  in. The mode is recorded as `[ota] enabled` in `openmv-ota.toml` and mirrored
  into the lock; changing it re-resolves the project, so set it at `new` time.

- **Keys provisioned.** A device trusts exactly the public keys baked into its
  firmware, and you cannot add a trusted key later without re-flashing. So `new
  --ota` provisions the *whole* key set up front (see [Keys](#keys) below) and
  writes it under `keys/`.

- **Per-board identity.** Each target board gets a `[targets.<BOARD>]` table for
  its `board_id` / `board_name` (see [Board identity](#board-identity)).

(The starter `app/` — including the `app_version` the build stamps into the image
— is scaffolded for every project, not just OTA; see
[The app folder](#the-app-folder).)

### Files an OTA project adds

On top of the files a plain project writes (settings, `.gitignore`, `README.md`,
and the starter `app/`), `new --ota` creates the keys and extends the config:

```
my-product/
├── openmv-ota.toml          # gains an [ota] section + per-board [targets.*] tables
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
enabled = true            # each partition holds a regular + golden image
signing_key_id = 256      # current OTA signing key (in keys/trusted_keys.json)
```

and each target board gets an active table for its identity (see
[Board identity](#board-identity)):

```toml
[targets.OPENMV_N6]
board_id   = 3064072142  # stable product id (auto-assigned; keep it once devices ship)
board_name = "my-product"  # human label; defaults to the product name, rename freely
```

### Keys

The key set has two roles, generated on the curve `--sig-alg` selects (ES256 →
P-256 by default; ES384/ES512 raise the curve and signature size):

| Role | id range | Default count | Purpose |
|---|---|---|---|
| `factory` | `0x0001`+ | 8 (`--factory-keys`) | One per manufacturing site; signs the golden image flashed at the factory. |
| `ota` | `0x0100`+ | 32 (`--ota-keys`) | The rotation pool; over-the-air updates are signed with one of these, rotated over the product's life. |

The two ranges are well-separated so the pools never collide at realistic counts.
The current signer is the first OTA key (`0x0100`), recorded as `signing_key_id`.
`build romfs` signs with that key, and a trailer records *which* key signed
(`key_id`) so the device picks the matching public key.

`keys/trusted_keys.json` is the committed public set the firmware build will bake
into its `TRUSTED_KEYS` table. Each entry is a key id, its COSE algorithm, its
role, and the public key as an uncompressed EC point in hex:

```json
{
  "schema": 1,
  "keys": [
    {"key_id": 1,   "alg": -7, "role": "factory", "pubkey": "04…"},
    {"key_id": 256, "alg": -7, "role": "ota",      "pubkey": "04…"}
  ]
}
```

Provision generously: because keys can't be added without re-flashing firmware,
the rotation pool is your entire future supply of OTA keys. `--ota-keys` below 4
warns. See [trailer.md](trailer.md) for the signature algorithms and the
`key_id` / `sig_alg` fields.

### Board identity

`board_id` is a `uint32` that names a product (the cross-flash guard), and
`board_name` is a human label for it. They live only in `openmv-ota.toml` (per
`[targets.<BOARD>]`) and are pure identity — **excluded from the lock and its
`config_digest`** — so setting a product id or renaming a board never trips drift
(unlike geometry overrides, which are firmware-relevant and *are* digested).
`build romfs` reads them and stamps them into `system.json` and the trailer: the
device's `board_id` guards against cross-flashing the wrong product; `board_name`
is metadata only.

**You never have to invent the number.** `project new --ota` auto-assigns each
board a stable `board_id`, derived deterministically from the product + board name
(distinct per board, reproducible). It's written into the config so it's frozen —
**keep it once devices ship**, because a device bakes its `board_id` in and rejects
any image whose id doesn't match; a later change would reject updates on fielded
devices. You can still override it (e.g. to match an existing product numbering),
and `build romfs` warns if you set it to `0` (guard off) or if two boards collide
on the same id.

A **non-OTA** project doesn't pin a `board_id` in its config (the guard only
applies to OTA), but `build romfs` still derives the same stable id and records it
in `system.json`, so a non-OTA app reads the same product identity — and nothing
changes when you later move to OTA. One app folder can target several boards or
products at once — each gets its own identity but shares the app and toolchain.

## Reconstructing a checkout

When someone clones a committed project, they have the config and lock but not
the firmware. `setup` clones the pinned firmware and installs its SDK, then
writes their `openmv-ota.local.toml`:

```bash
openmv-ota project setup ./my-product
```

It clones the remote at the locked commit into a local cache (override with
`--cache PATH` or `$OPENMV_OTA_CACHE`), checks out the submodules, runs `make
sdk`, and pip-installs the matching mpy-cross (the firmware's MicroPython version)
so the machine is ready to build. Pass `--no-install-sdk` to skip the toolchain
steps and only clone.

## Inspecting and updating

```bash
openmv-ota project show ./my-product          # the resolved snapshot
openmv-ota project show ./my-product --json   # the raw lock
openmv-ota project status ./my-product        # drift between lock and checkout
openmv-ota project verify ./my-product        # fail if anything has changed
openmv-ota project sync ./my-product          # re-resolve and rewrite the lock
```

`status` re-reads the current checkout and compares it to the lock, naming each
changed field. `sync` rewrites the lock from the current checkout when you intend
to move to a new firmware commit.

`status`, `verify`, and `sync` find the checkout from `openmv-ota.local.toml`, or
from `-f/--firmware`.

## Freezing the firmware

Once you build or release ROMFS images for a pegged firmware, the firmware must
not change — the images depend on the exact toolchain versions and board geometry
the project recorded. `verify` is the gate that enforces this:

```bash
openmv-ota project verify ./my-product
```

It exits 0 only when the checkout matches the lock in every recorded field **and**
the working tree is clean; otherwise it exits non-zero and lists what changed.
Uncommitted changes always fail, because the pinned commit does not capture them.
Run it in CI and before each image build.

Reading a project from Python verifies by default for the same reason — see below.

## What the lock records

`openmv-ota.toml` carries only what you choose (product metadata, target boards,
and whether the project is OTA). Everything else is resolved into
`openmv-ota.lock.json`:

- whether the project is OTA (which halves each partition's usable image budget);
- the firmware version, git remote, commit, branch, `git describe`, and whether
  the checkout was dirty;
- the MicroPython version, its commit, and the `.mpy` ABI version;
- the SDK version, and the resolved mpy-cross, Vela, and ST Edge AI versions;
- every submodule commit;
- per target (each board, and each of its targeted partitions): the arch and
  mpy-cross flags, the NPU type and its full compiler config (Vela / ST Edge AI
  arguments and config-file references), the alignment rules, and the partition
  and FRONT sizes.

Partition sizes come from the firmware's `boards/<BOARD>/board_config.h`. When a
board's size is build-variant conditional, the bundled default is used instead
and the source is recorded in `geometry_source`; set `partition_size` under a
`[targets.<BOARD>]` table in `openmv-ota.toml` to override it.

The lock's `config_digest` covers only the *firmware-relevant* config — boards,
geometry overrides like `partition_size`, and the OTA mode — so changing any of
those is drift you must `sync`. Pure-identity fields (`board_id`, `board_name`)
and metadata (product / vendor name, app version) are deliberately **excluded**,
so editing a product id or bumping your app version never invalidates the lock.

## Boards with multiple partitions

A board can have more than one ROMFS partition, each its own image. The AE3 has
two: the high-performance core (partition 0, OSPI) and the high-efficiency core
(partition 1, MRAM), with different sizes and different NPU configs. A target
defaults to partition 0; list the partitions to build images for both:

```toml
[targets]
boards = ["OPENMV_AE3"]

[targets.OPENMV_AE3]
partitions = [0, 1]
```

Each partition is resolved independently — its own size, FRONT size, alignment
rules, and NPU compiler config — and appears as a separate entry under
`targets.resolved`. From Python, select one with `board(name, partition)`, or
iterate `targets`.

## Reading a project from Python

`load_project` returns the lock plus this machine's resolved firmware path, SDK
home, and tool binary paths. It verifies that the checkout still matches the lock
(and is clean) first, raising if it does not, so a build never runs against a
changed firmware:

```python
from openmv_ota.project import load_project

p = load_project("./my-product")  # raises if the firmware has drifted
p.vela_path                       # path to the vela binary on this machine
p.targets                         # every (board, partition) target to build for
p.board("OPENMV_N6").front_size   # firmware-resolved FRONT partition size
p.board("OPENMV_N6").alignment_rules
p.board("OPENMV_AE3", 1).npu_config   # HE-core NPU type, args, and file refs
```

Pass `load_project("./my-product", verify=False)` to skip the check (reserved for
the firmware-update path, which does not yet exist).

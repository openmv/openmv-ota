# Projects

*[← 1 · The ROMFS](01-romfs.md) · [Index](00-introduction.md) · [3 · The lock →](03-the-lock.md)*

---

`openmv-ota project` ties a directory — your app plus a few settings files — to a
specific OpenMV firmware checkout, so every build uses exactly the tool versions
(mpy-cross, Ethos-U Vela, ST Edge AI) that firmware was built with. This page covers
creating a project and the app folder; [the next](03-the-lock.md) covers keeping that
peg honest over time. [OTA projects](04-ota-projects.md) and their
[signing keys](05-signing-keys.md) build on top.

## Creating a project

`new` pegs a project to a local OpenMV checkout:

```bash
openmv-ota project new ./my-product -f ~/openmv -b OPENMV_N6 -b OPENMV_AE3
```

It reads the checkout and the installed SDK, then writes `openmv-ota.toml` (the
config you edit), `openmv-ota.lock.json` (the resolved snapshot of the firmware —
[the next page](03-the-lock.md) is all about it), `openmv-ota.local.toml` (this
machine's checkout path), and a `.gitignore`. Commit the first two; the
`.gitignore` keeps the machine-local file out of the repository.

`new` expects the OpenMV SDK to be installed already. Pass `--install-sdk` to
download and install it (a pure-Python download + verify + extract of the
pinned bundle to `~/openmv-sdk-<version>` — no `make` required, which is what
lets the firmware build work on Windows), or `--sdk-home PATH` to point at an
SDK in a non-default location.

### Options

| Flag | Effect |
|---|---|
| `-f, --firmware PATH` | The local OpenMV checkout to peg to (required). |
| `-b, --board NAME` | A target board (repeatable, at least one). |
| `--product NAME` | Product name (defaults to the directory name). |
| `--vendor NAME` | Vendor name. |
| `--sdk-home PATH` | SDK install directory (default `~/openmv-sdk-<version>`). |
| `--install-sdk` | Download + install the SDK if it is missing. |
| `--allow-dirty` | Don't warn when the checkout has uncommitted changes. |
| `--ota` | Over-the-air project: split each partition and provision signing keys. |
| `--sig-alg {ES256,ES384,ES512}` | OTA signature algorithm (default `ES256` / P-256). |
| `--ota-keys N` | OTA rotation-pool size to provision (default 32). |
| `--factory-keys N` | Factory-key reserve to provision, one per manufacturing site (default 8). |
| `--force` | Overwrite an existing project. |
| `--ca PEM` | TLS roots the device trusts for OTA downloads, copied into the project and frozen into the firmware. Unset fetches the public Mozilla bundle — which does not fit the single-image classics (OpenMV2/3/4), so **they require this flag**. |
| `--key-passphrase-file FILE` | Passphrase (read from a file) encrypting the signing keys at rest; keys are never stored plaintext. |
| `--dev` | Throwaway dev keys with a cached random passphrase — nothing to manage, and the production build rail refuses them. |
| `--backup-passphrase-file FILE` | Auto-write an encrypted key backup using this passphrase (else a reminder is printed). |
| `--no-firmware-patches` | Don't auto-apply the OTA-required firmware patches; fail instead if the firmware lacks them. |

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
├── app-coprocessor/         # multi-core boards only: the slaved helper core's app
├── keys/                    # OTA only: trusted_keys.json (committed) + private/ (gitignored)
└── build/                   # gitignored: build output (one .romfs per target)
```

`new` writes the settings files, `.gitignore`, `README.md`, and a starter `app/`
— a placeholder `main.py` and a `settings.json` carrying your app version (see
[The app folder](#the-app-folder)). Replace `main.py` with your code; an OTA
project (`--ota`) additionally provisions `keys/` (see
[OTA projects](04-ota-projects.md)). `openmv-ota build romfs` compiles the app and
writes images to `build/`. Commit everything except `openmv-ota.local.toml`,
`keys/private/`, and `build/`, which the generated `.gitignore` already excludes.
(`app/` and `build/` are the defaults; `build romfs` takes `--app` and `--output`
to use other directories.)

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
[build.md](06-building.md)), making this file the one place a version is defined.

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

For a **multi-core board** (e.g. AE3), `new` also scaffolds a second folder,
`app-coprocessor/`, holding the slaved helper core's app. It has the same shape
(`main.py`, `settings.json`, `lib/`) but is always built as a *plain* romfs, never
OTA — see [Multi-core boards](04-ota-projects.md#multi-core-boards-a-coprocessor-partition).

### `system.json` (generated, read-only)

Keep *user-editable* settings in `settings.json`. *Derived* values — board
identity and build provenance — must not be hand-edited, so the build generates a
separate, read-only **`system.json`** into every image (OTA or not) at
`/rom/system.json`:

```json
{
  "product": "orchard-sentry",
  "board": "OPENMV_N6",
  "product_id": 4097,
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
(per-board `product_id` / `board_name`); for an OTA image the signed
[trailer](../reference/trailer.md) also carries a verbatim copy, so host tools can read it
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
product_id   = 1001
board_name = "My Product Lite"

[targets.OPENMV_AE3]
product_id   = 1002
board_name = "My Product Pro"
```

Set `board_name` only when you ship the one product on multiple boards and want
them named apart; otherwise leave it and `product` carries the name.

---

*[← 1 · The ROMFS](01-romfs.md) · [Index](00-introduction.md) · [3 · The lock →](03-the-lock.md)*

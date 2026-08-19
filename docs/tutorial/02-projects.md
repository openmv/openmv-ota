# Projects

*[← 1 · The ROMFS](01-romfs.md) · [Index](00-introduction.md) · [3 · The lock →](03-the-lock.md)*

---

`openmv-ota project` ties a directory — your app plus a few settings files — to a
specific OpenMV firmware checkout, so every build uses exactly the tool versions
(mpy-cross, Ethos-U Vela, ST Edge AI) that firmware was built with. This page covers
creating a project and the app folder.

## Creating a project

`new` pegs a project to a local OpenMV checkout:

```bash
openmv-ota project new ./my-product -f ~/openmv -b OPENMV_N6 -b OPENMV_AE3
```

It reads the checkout and the installed SDK, then writes `openmv-ota.toml` (the
config you edit), `openmv-ota.lock.json` (the resolved snapshot of the firmware),
`openmv-ota.local.toml` (this
machine's checkout path), and a `.gitignore`. Commit the first two; the
`.gitignore` keeps the machine-local file out of the repository.

`new` expects the OpenMV SDK to be installed already. Pass `--install-sdk` to
download and install it — a pure-Python download + verify + extract of the
pinned bundle to `~/openmv-sdk-<version>`. `--sdk-home PATH` points at an SDK
in a non-default location.

### Options

| Flag | Effect |
|---|---|
| `-f, --firmware PATH` | The local OpenMV checkout to peg to (required). |
| `-b, --board NAME` | A target board (repeatable, at least one). |
| `--product NAME` | Product name (defaults to the directory name). |
| `--vendor NAME` | Vendor name, written into the scaffolded `settings.json`. |
| `--sdk-home PATH` | SDK install directory (default `~/openmv-sdk-<version>`). |
| `--install-sdk` | Download + install the SDK if it is missing. |
| `--allow-dirty` | Don't warn when the checkout has uncommitted changes. |
| `--force` | Re-run `new` over an existing project (refused otherwise). An existing `app/` is never overwritten. |

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
├── app-coprocessor/         # multi-core boards only: the second core's app
└── build/                   # gitignored: build output (one image per target)
```

`new` writes the settings files, `.gitignore`, `README.md`, and a starter `app/`
— a placeholder `main.py` and a `settings.json` carrying your app version.
Replace `main.py` with your code. Commit everything except
`openmv-ota.local.toml` and `build/`; the generated `.gitignore` already
excludes them.

## The app folder

Every project is scaffolded with a starter `app/`:

```
app/
├── main.py        # placeholder; replace with your code
├── settings.json  # your app's version and settings
└── lib/           # your own importable modules
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
  "vendor": "Acme Robotics"
}
```

It is packed into the ROMFS image, so the app can read it on-device (e.g.
`json.load(open("/rom/settings.json"))`) — useful in any project for reporting a
version or carrying configuration. Bump `app_version` (a `major.minor.patch`
semver) for each release.

For a **multi-core board** (e.g. AE3), `new` also scaffolds a second folder,
`app-coprocessor/`, holding the slaved helper core's app. It has the same shape
(`main.py`, `settings.json`, `lib/`) and is built into an image of its own.

### `system.json` (generated, read-only)

Keep *user-editable* settings in `settings.json`. *Derived* values — board
identity and build provenance — must not be hand-edited, so the build generates a
separate, read-only **`system.json`** into every image at
`/rom/system.json`:

```json
{
  "product": "orchard-sentry",
  "board": "OPENMV_N6",
  "product_id": 2937722637,
  "board_name": "OrchardSentry Pro",
  "app_version": "1.0.0",
  "vendor": "Acme Robotics",
  "ota": false,
  "firmware": {"version": "5.0.0", "commit": "9f2c1ab3d4e5f60718293a4b5c6d7e8f90a1b2c3"},
  "micropython": "1.28.0",
  "toolchain": {"mpy_cross": "1.28.0", "vela": "3.12.0", "stedgeai": "2.1.0", "sdk": "1.6.0"}
}
```

This gives the app **one consistent read path for system state in every build**
— `json.load(open("/rom/system.json"))`. It is composed
from the lock (firmware / MicroPython / toolchain provenance) and the config
(per-board `product_id` / `board_name`). `system.json` is generated into the built image only —
never into your `app/` source — so there is nothing to edit or accidentally commit.
(The name is reserved; a `system.json` in your `app/` is overwritten.)

### Product name vs board name

`system.json` carries four identity fields, and how `product` and `board_name`
relate depends on how many boards a project targets:

- **`board`** is always the canonical board key (`OPENMV_N6`) — the technical
  target, never renamed.
- **`product`** is your project/product name (`[product].name`, defaulting to the
  directory name). It is the same for every board the project builds.
- **`product_id`** is a number derived *for* you — the CRC32 of
  `"product:board"` — so it is stable, distinct per board, and reproducible: two
  machines (or a rebuilt config) derive the same value. You never invent it.
- **`board_name`** is a human label, set per board under `[targets.<BOARD>]`. If
  you don't set it, it **defaults to `product`**.

For a **single-board project**, `product` and `board_name` are the same string by
default — you can ignore `board_name` and read `product`. For a **multi-board
project** (one app folder targeting several boards), `product` stays constant
while you can give each board its own `board_name` to distinguish the variants in
the field — by **adding a `[targets.<BOARD>]` table** (a plain project's config
isn't scaffolded with any) — e.g. one app built for two boards sold as "My Product
Lite" and "My Product Pro":

```toml
[product]
name = "my-product"          # product, shared by every board

[targets]
boards = ["OPENMV_N6", "OPENMV_AE3"]

[targets.OPENMV_N6]
board_name = "My Product Pro"

[targets.OPENMV_AE3]
board_name = "My Product Lite"
```

Set `board_name` only when you ship the one product on multiple boards and want
them named apart; otherwise leave it and `product` carries the name.

A plain project derives its identity fresh at every build — rename the product
and the derived `product_id` simply follows; there is nothing to update.

---

*[← 1 · The ROMFS](01-romfs.md) · [Index](00-introduction.md) · [3 · The lock →](03-the-lock.md)*

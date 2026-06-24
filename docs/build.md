# build

`openmv-ota build` compiles a project's app and produces deployable images.
`build romfs` (an OTA payload), `build factory-romfs` (the full dual-slot
partition image flashed at the factory), and `build firmware` (the device
firmware, with an OTA boot script frozen in for OTA projects) are available now.

## build romfs

`build romfs` reads a project, compiles the app the way the pegged firmware
expects, and packs a ROMFS image for each target board:

```bash
openmv-ota build romfs ./my-product
```

For each target it compiles every `.py` to `.mpy` with the project's mpy-cross,
converts NPU models with the project's Vela (AE3) or ST Edge AI (N6), packs the
result into a ROMFS image with the board's alignment rules, and checks it against
the available capacity. For a **non-OTA** project the output is the ROMFS body,
`<project>/build/<board>.img` (one per target; a board with more than one partition
gets `<board>-p<index>.img`). An **OTA** project instead writes a signed bundle,
`<board>.zip` (see [OTA signing](#ota-signing) below).

Every image also gets a generated, read-only `system.json` at `/rom/system.json` —
board identity (`board`, `board_id`, `board_name`, `product`), the app version, and
build provenance (firmware / MicroPython / toolchain versions) — composed from the
lock and the per-board config. It gives the app one consistent way to read its own
identity and provenance, the same in a non-OTA and an OTA build. See
[project.md](project.md#systemjson-generated-read-only).

The capacity is the whole partition for a single-image project, or half the
partition less two flash erase blocks (a status sector + a trailer; 8 KiB on
OTA-capable boards) for an OTA project (`project new
--ota`) — each OTA partition holds a regular image and a golden fallback. The build
summary reports the percentage of whichever bound applies.

The app source defaults to `<project>/app`; pass `--app` to use another directory.
The project must match its lock and be clean — `build romfs` refuses to run
against a firmware checkout that has drifted (run `openmv-ota project status` to
see the difference, or `openmv-ota project sync` to re-peg).

This is distinct from `openmv-ota romfs pack`, which packs a directory verbatim
with no compilation.

### OTA signing

For an OTA project (`project new --ota`), `build romfs` does more than pack the
body: it stamps and signs a **trailer** onto each image, so the output is a
verifiable, anti-rollback OTA image rather than a bare ROMFS body. No extra flags
— the signing context comes from the project:

- **App version → payload version.** The app version is read from
  `app/settings.json` (`{"app_version": "1.0.0", …}`) — the single source of truth
  both the on-device app and the build read. The semver is encoded into the
  trailer's `payload_version` as `(major<<24)|(minor<<16)|(patch<<8)`, the
  monotonic anti-rollback counter. Bump it in `settings.json` for each release.
- **Signed with the project's current OTA key.** The signer is
  `[ota].signing_key_id` from `openmv-ota.toml`; its private key is loaded from
  `keys/private/ota-<id>.pem`, and the trailer records `key_id` + the COSE
  algorithm so the device selects the matching trusted public key.
- **Per-board identity + provenance stamped in.** `board_id` / `board_name` come
  from each `[targets.<BOARD>]` table; the firmware / MicroPython / toolchain / SDK
  versions and commit come from the lock. These are exactly the `system.json`
  fields, and the trailer's JSON metadata carries a **verbatim copy of
  `system.json`** so host tools can read the image's identity without mounting the
  ROMFS. `min_platform_version` is the pegged firmware's version code.

An OTA build writes a single **bundle**, `<board>.zip`, containing two entries:

| Entry | What |
|---|---|
| `romfs.img` | the ROMFS body (mounted at `/rom`, written to the slot start) |
| `trailer.bin` | the signed trailer (written to the slot's last erase block) |

One file is easier to flash / upload / track, but the pieces stay separate
*entries* — a zip is random-access, so the update server reads `trailer.bin`
(version / `board_id` / signature, including its verbatim copy of `system.json`)
without touching the multi-MB body. The trailer *is* the manifest, so there is no
separate `manifest.json` to keep in sync. The device never gets the zip (it can't
hold the body in RAM to unzip): a server unbundles and streams the body + trailer
separately, exactly as they're placed on-flash. Every trailer field is final and
signed, including `pad_size` (the `0xFF` gap to the status sector, computed from the
slot geometry) and the crc32. The build summary reports the body size against the
OTA-slot budget. See [trailer.md](trailer.md) for the on-flash format.

`build romfs` fails the build (exit 1) if the OTA signing context is incomplete:
a missing or unreadable `app/settings.json`, a missing or non-semver
`app_version`, a `signing_key_id` that isn't in `keys/trusted_keys.json`, or a
missing private key (only the signing machine has `keys/private/`). It *warns*
(but still builds) if a target's `board_id` is `0` — which only happens if you
override the auto-assigned id to `0`, turning the cross-flash guard off — or if two
boards share the same `board_id` (the guard can't tell them apart).

### Compiling

`.py` files are compiled to `.mpy`. Models (`.tflite`, `.lite`, `.onnx`) are
converted for the board's NPU; a model that is already converted is packed
unchanged. Pass `--no-compile-py` to pack `.py` as source, or
`--no-convert-models` to pack models as-is.

`build romfs` runs mpy-cross. It uses the binary the firmware build produced if
present; otherwise it uses a pip-installed `mpy_cross` (`python -m mpy_cross`, as
the IDE does), so no C compiler is needed. `openmv-ota project setup` installs the
matching version; you can also install it yourself — the version matches the
firmware's MicroPython version, which `openmv-ota project show` reports:

```bash
pip install mpy-cross==1.28.0    # use your firmware's MicroPython version
```

If neither is available, `build romfs` prints the command to run. Pass
`--no-compile-py` to skip compilation entirely.

### Tool arguments

The compilers are run with the board's pegged arguments. To add your own, use the
per-tool flags (repeatable). For a value that begins with `-`, use the `=` form so
it is not mistaken for an option:

```bash
openmv-ota build romfs ./my-product --vela-arg=--verbose-all --mpy-arg=-O2
```

Optimisation differs per tool: Vela takes a mode, ST Edge AI takes a level.

| Flag | Effect |
|---|---|
| `-b, --board NAME` | Build only this board (repeatable; default: all targets). |
| `-p, --partition N` | Build only this partition. |
| `--app DIR` | App source directory (default: `<project>/app`). |
| `-o, --output DIR` | Output directory (default: `<project>/build`). |
| `--no-compile-py` | Pack `.py` as source instead of compiling. |
| `--no-convert-models` | Pack models as-is instead of converting. |
| `--mpy-arg ARG` | Extra mpy-cross argument (repeatable). |
| `--vela-arg ARG` | Extra Vela argument (repeatable). |
| `--stedgeai-arg ARG` | Extra ST Edge AI argument (repeatable). |
| `--vela-optimise {Performance,Size}` | Vela optimisation (default: Performance). |
| `--stedgeai-optimization {0,1,2,3}` | ST Edge AI level (default: 3 = max). |
| `-f, --firmware PATH` | Firmware checkout override. |
| `--allow-oversize` | Warn instead of failing when an image exceeds the partition. |
| `--keep-build-dir` | Keep the staging directory for inspection. |

## build factory-romfs

`build romfs` produces an OTA *payload* — the body + trailer a server streams to a
device that is already running. `build factory-romfs` produces the **whole ROMFS
partition image flashed at the factory**: the complete on-flash layout a board
needs before it has ever taken an update.

```bash
openmv-ota build factory-romfs ./my-product
```

The output is `<project>/build/<board>-factory.img`, sized to the exact partition
and ready to write at the partition's offset. It composes the same compiled body
into the partition's two slots:

| Slot | Contents | Status sector | Role |
|---|---|---|---|
| **FRONT** | body + pad + status + trailer | `pending` + `tried` + `confirmed` | the mutable slot OTA writes to; ships already-confirmed so the first boot mounts it |
| **BACK** | body + pad + status + trailer | `confirmed` only | the golden fallback, never overwritten by OTA |

The partition is split in half (the FRONT half aligned down to the flash erase
block); each slot ends with a one-block **status sector** and a one-block
**trailer**, with `0xFF` padding filling the gap between the body and those two
blocks. Both slots carry the same body but their own trailer, each signed
independently with the recorded `pad_size`.

The two status sectors are what make the slots distinguishable on a fresh board.
FRONT ships in the post-OTA *confirmed* state — `pending`, `tried`, and `confirmed`
markers all set — so the device's `boot.py` mounts it on the very first boot
without a trial cycle. BACK carries only `confirmed`: that is the golden-slot
shape, and `boot.py` will not mount a *FRONT* in that shape (a confirmed-only
FRONT means a torn or invalid initial flash), so the markers also encode which slot
is which.

### Signed with a factory key

A factory image is signed with a **factory** key, not an OTA key. The signer
defaults to factory key `0x0001`; pass `--factory-key` to select another
(`--factory-key 0x0002`). The key must be a `factory`-role entry in
`keys/trusted_keys.json` and have its private key in
`keys/private/factory-<id>.pem`; signing with an `ota`-role key is refused.

**A factory key is *yours*, not the factory's.** You hold it, you sign with it, and
you ship the manufacturer the finished `<board>-factory.img` — a flat binary they
write to flash. They never receive a private key, the project, or this tool; a
contract manufacturer is a flashing station, not a build host. **Never hand a
private key (`keys/private/*.pem`) to anyone.** If a third party genuinely must
sign on their own hardware, sign through a service or HSM where the key never
leaves your control rather than copying a `.pem` to their machine.

Given that, the per-site `factory_key` id is for **attribution, not key
isolation** — distinct ids let you tell which production run cut a given image, and
let you `revoke` one run's key without touching the others. It is *not* an
anti-overproduction control: a manufacturer holding a signed image can reflash it
onto any number of boards, and a per-site key does nothing to stop that. Metering
how many devices are built is the job of per-device registration (each unit gets a
unique id-bound credential at flash time), which is separate from image signing.
Factory keys, like OTA keys, are assigned and `revoke`-able but **not rotated** —
you retire a compromised run's id, you don't roll a live one.

Everything else — compilation, model conversion, per-board identity and
provenance, the `system.json` copy in each trailer, the drift check — is identical
to `build romfs`. The capacity check is against a single factory slot (half the
partition less the two erase blocks); an app that doesn't fit a slot fails the
build. `build factory-romfs` requires an OTA project (`project new --ota`); on a
non-OTA project it errors. It takes the same compilation / board / output flags as
`build romfs`, plus `--factory-key`.

## build firmware

`build firmware` builds the device firmware for each board by running the firmware
repo's own `make` in the pegged checkout, so the result is byte-for-byte what the
firmware build produces:

```bash
openmv-ota build firmware ./my-product
```

For each board it runs `make TARGET=<board>` and copies the result into
`<project>/build/`: `<board>.bin` for an stm32 board (the bootloader-combined
`openmv.bin` if the board builds one), or a per-core `<board>-M55_HP.bin` /
`<board>-M55_HE.bin` for an Alif board (its `firmware.toc` is written by the
bootloader and is not collected). Firmware is built per board, not per partition,
so a board with multiple ROMFS partitions still builds one firmware.

The behavior follows the project's OTA flag automatically — there is no separate
option:

- **Non-OTA project:** just builds the firmware.
- **OTA project:** additionally freezes an OTA **`boot.py`** into the image. It does
  this without copying anything into or editing the firmware tree: it generates a
  temporary *wrapper manifest* that `include`s the board's own manifest and adds the
  boot script, and points the build at it with `make FROZEN_MANIFEST=<wrapper>`. The
  frozen `boot.py` runs after the board's stock `_boot.py` (the stock boot is left
  untouched). *(The boot script is currently a placeholder; the on-device
  trailer-parse, signature/SHA verification, and FRONT/BACK slot selection land in a
  later step.)*

The build is **clean by default** (`make clean` then build). A stale `build/<board>`
tree fails at link with a misleading `__cyg_profile_func_enter` error — imlib is
compiled with `-finstrument-functions` — that has nothing to do with anything we
inject, so a clean build avoids it. Pass `--incremental` to skip the clean for fast
iteration when the tree is known good.

Building firmware needs a firmware toolchain (`make` plus the board's cross
compiler); `build firmware` shells out to it and reports a non-zero exit if it is
missing or the build fails.

| Flag | Effect |
|---|---|
| `-b, --board NAME` | Build only this board (repeatable; default: all boards). |
| `-o, --output DIR` | Output directory (default: `<project>/build`). |
| `-j, --jobs N` | Parallel make jobs (default: CPU count). |
| `--incremental` | Skip the clean rebuild (only when the tree is known good). |
| `-f, --firmware PATH` | Firmware checkout override. |
| `--keep-build-dir` | Keep the generated wrapper manifest dir (OTA builds) for inspection. |

## Inspecting and verifying an OTA image

Two read-only commands operate on a built OTA image. They take the `<board>.zip`
bundle directly, or the loose `romfs.img` / `trailer.bin` (e.g. if you've
unzipped). They live under `build` because they validate build outputs.

### build inspect

```bash
openmv-ota build inspect build/OPENMV_N6.zip
openmv-ota build inspect build/OPENMV_N6.zip --json
```

Decodes the signed trailer and prints it: product / board / `board_id` /
`board_name`, the app version (and the `payload_version` / `rollback_floor` /
`min_platform_version` it encodes, shown as semver), the signing key and
algorithm, the body size + SHA-256, and a provenance line (firmware / MicroPython
/ toolchain). `--json` dumps the full structure, including the complete metadata
blob, for scripting. It does no crypto — it just reads the trailer.

### build verify

```bash
openmv-ota build verify build/OPENMV_N6.zip
```

The host-side **authenticity + integrity** gate — the mirror of what the device's
`boot.py` checks, for use in CI or before publishing. It confirms the trailer
parses, the signing `key_id` is in the trusted set **and not revoked**, the
algorithm matches, the **signature verifies** over the signed region, and the body
matches the signed size + SHA-256. Exit 0 on success, 1 on a verification failure
(with the reason), 2 on a bad argument. Pass the `.zip` (one argument) or the loose
`romfs.img trailer.bin` (two). Trusted keys come from `--trusted-keys` (default
`keys/trusted_keys.json`), so running it from a project root just works.

It deliberately does **not** check the device-relative fields — `board_id` against
a device, `payload_version` anti-rollback against the installed image,
`min_platform_version` against the running firmware — because those need a device,
not a host. Those remain `boot.py`'s job.

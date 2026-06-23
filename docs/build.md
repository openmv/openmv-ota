# build

`openmv-ota build` compiles a project's app and produces deployable images.
`build romfs` is available now; `build firmware` is reserved.

## build romfs

`build romfs` reads a project, compiles the app the way the pegged firmware
expects, and packs a ROMFS image for each target board:

```bash
openmv-ota build romfs ./my-product
```

For each target it compiles every `.py` to `.mpy` with the project's mpy-cross,
converts NPU models with the project's Vela (AE3) or ST Edge AI (N6), packs the
result into a ROMFS image with the board's alignment rules, and checks it against
the available capacity. The output is written to `<project>/build/<board>.romfs`
(one per target; a board with more than one partition gets `<board>-p<index>.romfs`).
An OTA project additionally writes a signed `<board>.trailer` per target (see
[OTA signing](#ota-signing) below).

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

An OTA build writes **two files** per target: `<board>.romfs` (the ROMFS body,
identical to a non-OTA build) and `<board>.trailer` (the standalone signed
trailer). They're kept separate because on-device they go to different places — the
body to the start of the slot, the trailer to the slot's last erase block — and
because the trailer carries a copy of `system.json`, so the update server reads an
image's version / `board_id` / signature straight from `<board>.trailer` without
touching the body. Every trailer field is final and signed, including `pad_size`
(the `0xFF` gap to the status sector, computed from the slot geometry) and the
crc32. The build summary reports the body size against the OTA-slot budget (the
trailer and status sectors are accounted for in the budget). See
[trailer.md](trailer.md) for the on-flash format.

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

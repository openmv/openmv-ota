# Building

*[← 5 · Signing keys](05-signing-keys.md) · [Index](00-introduction.md) · [7 · Factory & firmware →](07-factory-and-firmware.md)*

---

This page is `build romfs` — the build engine every other `build` verb runs
internally. For a **plain project it is the whole build**: the image it packs is
what you flash. For an **OTA project** it writes the signed bundle the factory
and release artifacts are composed from — you will usually run those commands
instead, and meet this machinery through them.

## build romfs

`build romfs` reads a project, compiles the app the way the pegged firmware
expects, and packs a ROMFS image for each target board:

```bash
openmv-ota build romfs ./my-product
```

For a **non-OTA** project the output is the ROMFS body,
`<project>/build/<board>-romfs.img`. An **OTA** project writes a signed bundle,
`<board>-romfs.zip`, instead. A multi-core board also builds its coprocessor
partition as a plain `<board>-coprocessor-romfs.img`, and an OTA build nests that
image inside the main one for
[`openmv_ota.sync()`](04-ota-projects.md#multi-core-boards-a-coprocessor-partition)
to apply on-device.

Every image carries the generated, read-only
[`/rom/system.json`](02-projects.md#systemjson-generated-read-only) — identity and
provenance, composed from the config and the lock. Capacity is the whole partition
for a plain project and the [OTA slot budget](04-ota-projects.md#what---ota-changes)
otherwise; the build summary reports usage against whichever bound applies.

The app source defaults to `<project>/app`; pass `--app` for another directory.

The pegged firmware checkout must match the lock and be clean. The app is
compiled with that checkout's own tools — the `.mpy` bytecode must match the
firmware it will run on — and the image stamps the lock's provenance, so a
drifted tree would build an image whose claims don't describe its bytes.
`build romfs` refuses it; `project status` shows the difference and
`project sync` re-pegs. This is distinct from `openmv-ota romfs pack`, which
packs a directory verbatim with no compilation.

### Compiling

`.py` files are compiled to `.mpy` with the project's mpy-cross; models
(`.tflite`, `.lite`, `.onnx`) are converted for the board's NPU with the
project's Vela (AE3) or ST Edge AI (N6). A model that is already converted is
packed unchanged.

mpy-cross is the binary the firmware build produced when present, else a
pip-installed `mpy_cross` (as the IDE uses — no C compiler needed).
`project setup` installs the matching version; to install it yourself, match the
firmware's MicroPython version (`project show` reports it):

```bash
pip install mpy-cross==1.28.0
```

If neither is available, `build romfs` prints that command.

### Tool arguments

The compilers run with the board's pegged arguments; add your own with the
per-tool flags (repeatable — use the `=` form for a value that begins with `-`):

```bash
openmv-ota build romfs ./my-product --vela-arg=--verbose-all --mpy-arg=-O2
```

| Flag | Effect |
|---|---|
| `-b, --board NAME` | Build only this board (repeatable; default: all targets). |
| `--app DIR` | Main app source directory (default: `<project>/app`). A coprocessor partition always builds from `<project>/app-coprocessor`. |
| `-o, --output DIR` | Output directory (default: `<project>/build`). |
| `--no-compile-py` | Pack `.py` as source instead of compiling. |
| `--no-convert-models` | Pack models as-is instead of converting. |
| `--mpy-arg ARG` | Extra mpy-cross argument (repeatable). |
| `--vela-arg ARG` | Extra Vela argument (repeatable). |
| `--stedgeai-arg ARG` | Extra ST Edge AI argument (repeatable). |
| `--vela-optimise {Performance,Size}` | Vela optimisation mode (default: Performance). |
| `--stedgeai-optimization {0,1,2,3}` | ST Edge AI level (default: 3 = max). |
| `-f, --firmware PATH` | Firmware checkout override. |
| `--allow-oversize` | Warn instead of failing when an image exceeds the budget. |
| `--keep-build-dir` | Keep the staging directory for inspection. |

---

*[← 5 · Signing keys](05-signing-keys.md) · [Index](00-introduction.md) · [7 · Factory & firmware →](07-factory-and-firmware.md)*

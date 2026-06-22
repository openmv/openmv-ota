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
the partition size. The output is written to `<project>/build/<board>.romfs` (one
per target; a board with more than one partition gets `<board>-p<index>.romfs`).

The app source defaults to `<project>/app`; pass `--app` to use another directory.
The project must match its lock and be clean — `build romfs` refuses to run
against a firmware checkout that has drifted (run `openmv-ota project status` to
see the difference, or `openmv-ota project sync` to re-peg).

This is distinct from `openmv-ota romfs pack`, which packs a directory verbatim
with no compilation.

### Compiling

`.py` files are compiled to `.mpy`. Models (`.tflite`, `.lite`, `.onnx`) are
converted for the board's NPU; a model that is already converted is packed
unchanged. Pass `--no-compile-py` to pack `.py` as source, or
`--no-convert-models` to pack models as-is.

`build romfs` uses the mpy-cross binary the firmware build produced. If it is not
present, build the firmware first or pass `--no-compile-py`.

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

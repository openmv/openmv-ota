# The `romfs` image tool

`openmv-ota romfs` builds and inspects OpenMV ROMFS images — the read-only
filesystem the firmware mounts at `/rom`. It is the core, dependency-free
foundation the OTA layers build on, but it is useful on its own: anywhere you'd
use the IDE's "build ROMFS" feature, from the command line and in CI.

The format is a faithful port of the OpenMV IDE's reference writer/reader and
reproduces real IDE-built images byte-for-byte. Files are packed **verbatim** —
this tool does not (yet) compile `.py` with mpy-cross or convert models for the
NPU (Vela / ST Edge AI); those are a planned layer. Pre-convert such assets, or
let the IDE do it, then pack the result here.

## Why alignment matters

OpenMV maps some ROMFS files in place (notably NPU model blobs), so their bytes
must start on a specific boundary. Each board declares per-extension alignment
rules (e.g. the N6 wants `.tflite` on 32-byte boundaries; most boards use 16).
`--board` applies those rules automatically; the tool tracks absolute offsets
through nested directories so every payload lands correctly.

## Commands

```bash
# Pack a directory (its contents become the ROMFS root).
openmv-ota romfs build ./app -o app.romfs --board OPENMV_N6

# Unpack back to a directory.
openmv-ota romfs extract app.romfs -o ./out

# Inspect.
openmv-ota romfs ls app.romfs -l        # sizes, offsets, suffixes
openmv-ota romfs info app.romfs         # summary
openmv-ota romfs cat app.romfs main.py  # one file's bytes to stdout
openmv-ota romfs verify app.romfs --board OPENMV_N6   # parse + alignment check

# Board config.
openmv-ota romfs boards                 # list
openmv-ota romfs boards OPENMV_N6       # one board's partitions + rules
```

Use `-` as the image path to read from stdin or write to stdout, e.g.
`openmv-ota romfs build ./app -o - --board OPENMV_N6 | openmv-ota romfs verify -`.

## `build` options

| Flag | Effect |
|---|---|
| `-b, --board NAME` | Apply the board's alignment rules and partition capacity. |
| `-p, --partition N` | Select a partition on multi-partition boards (e.g. AE3). |
| `--align EXT=N` | Override the alignment for one extension, on top of the board defaults (repeatable). Also `--alignment`. |
| `--default-alignment N` | Fallback alignment for extensions with no rule (default 4). |
| `--no-board-rules` | Ignore the board's rules; use only `--align`. |
| `--exclude GLOB` | Skip entries matching GLOB (repeatable). |
| `--no-default-excludes` | Keep `__pycache__`, `*.pyc`, `.git`, `.DS_Store`, … (excluded by default). |
| `--follow-symlinks` | Follow symlinks instead of skipping them. |
| `--max-size BYTES` | Capacity to check against (default: the board partition size). Accepts `0x…` / `K`/`M`/`G`. |
| `--allow-oversize` | Warn instead of failing when the image exceeds capacity. |
| `-q, --quiet` | Suppress the summary. |

`--board` sets the defaults; per-type flags layer on top. Output is
deterministic: directory entries are visited in sorted order, so the same input
always produces byte-identical bytes (good for reproducible builds).

## Board config source

Board alignment rules and partition sizes are bundled in
`src/openmv_ota/data/boards.json`, extracted from the OpenMV IDE's
`share/qtcreator/firmware/settings.json`. Boards are fixed hardware, so this
duplication is intentional and stable. The `mpy_args` and `npu` entries are
carried through for the future model-compile layer and are unused by the core
builder.

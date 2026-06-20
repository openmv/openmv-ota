# The `romfs` image tool

`openmv-ota romfs` builds and inspects OpenMV ROMFS images — the read-only
filesystem the firmware mounts at `/rom`. It is the generic, dependency-free
foundation the OTA layers build on, but it stands on its own: use it anywhere
you'd build a ROMFS image, from the command line or in CI.

> These are brief developer notes. Comprehensive, user-facing documentation for
> the whole stack will live in openmv-doc.

## Scope

This tool is deliberately **generic**. It knows about the ROMFS container format
and per-board memory alignment — nothing more.

**It does:**

- pack a directory tree into a ROMFS image, and unpack one back to a directory;
- apply each board's per-extension alignment rules so memory-mapped assets land
  on the right boundary;
- list, read, summarise, and validate images.

**It does not** (these are separate, higher-level layers — see
[Relationship to the OTA layers](#relationship-to-the-ota-layers)):

- sign images, write trailers, or compose factory/OTA slots;
- compile `.py` with mpy-cross or convert models for the NPU (Vela / ST Edge AI);
- know anything about update servers, versions, or rollback.

Files are packed **verbatim**. Pre-convert `.py`/model assets (or let the IDE do
it), then pack the result here.

## Format

The image format is a faithful port of the OpenMV IDE's reference writer/reader
and reproduces real IDE-built images byte-for-byte. Output is deterministic:
directory entries are visited in sorted order, so the same input always produces
identical bytes (good for reproducible builds).

### Why alignment matters

OpenMV maps some ROMFS files in place (notably NPU model blobs), so their bytes
must start on a specific boundary. Each board declares per-extension alignment
rules — the N6 wants `.tflite` on 32-byte boundaries; most boards use 16. The
tool tracks absolute offsets through nested directories so every payload lands
correctly. `--board` applies the rules automatically; `romfs verify` checks them.

## Commands

```bash
# Pack a directory (its contents become the ROMFS root).
openmv-ota romfs build ./app -o app.romfs --board OPENMV_N6

# Unpack back to a directory.
openmv-ota romfs extract app.romfs -o ./out

# Inspect.
openmv-ota romfs ls app.romfs -l         # sizes, offsets, suffixes
openmv-ota romfs info app.romfs          # summary
openmv-ota romfs cat app.romfs main.py   # one file's bytes to stdout
openmv-ota romfs verify app.romfs --board OPENMV_N6   # parse + alignment check

# Board config.
openmv-ota romfs boards                   # list supported boards
openmv-ota romfs boards OPENMV_AE3        # one board's partitions + rules
```

Run `openmv-ota romfs boards` for the list of supported board names. Multi-
partition boards (e.g. the AE3, which has separate OSPI and MRAM partitions) take
`-p/--partition`.

Use `-` as the image path to read from stdin or write to stdout, e.g.
`openmv-ota romfs build ./app -o - --board OPENMV_N6 | openmv-ota romfs verify -`.

## `build` options

| Flag | Effect |
|---|---|
| `-b, --board NAME` | Apply the board's alignment rules and partition capacity. |
| `-p, --partition N` | Select a partition on multi-partition boards. |
| `--align EXT=N` | Override the alignment for one extension, on top of the board defaults (repeatable). Also `--alignment`. |
| `--default-alignment N` | Fallback alignment for extensions with no rule (default 4). |
| `--no-board-rules` | Ignore the board's rules; use only `--align`. |
| `--exclude GLOB` | Skip entries matching GLOB (repeatable). |
| `--no-default-excludes` | Keep `__pycache__`, `*.pyc`, `.git`, `.DS_Store`, … (excluded by default). |
| `--follow-symlinks` | Follow symlinks instead of skipping them. |
| `--max-size BYTES` | Capacity to check against (default: the board partition size). Accepts `0x…` / `K`/`M`/`G`. |
| `--allow-oversize` | Warn instead of failing when the image exceeds capacity. |
| `-q, --quiet` | Suppress the summary. |

`--board` sets the defaults; per-type flags layer on top.

## Board config source

Board alignment rules and partition sizes are bundled in
`src/openmv_ota/data/boards.json`, extracted from the OpenMV IDE's
`share/qtcreator/firmware/settings.json`. Boards are fixed hardware, so this
duplication is intentional and stable. The `mpy_args` and `npu` entries are
carried through for the future model-compile layer and are unused by this tool.

## Relationship to the OTA layers

This image tool is **Layer 1**. The OTA layers sit on top of it and are the only
place higher-level concerns live:

- **signing / slot composition** wraps a built image with a signed trailer and
  arranges the FRONT/BACK slots;
- **model compilation** converts assets *before* they are packed here;
- **the update server + on-device SDK** deliver and install images.

None of that leaks into this tool: `romfs build` always produces a plain,
unsigned image. See
[../openmv-romfs-ota-concept-plan.md](../openmv-romfs-ota-concept-plan.md).

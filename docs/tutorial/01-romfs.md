# The ROMFS

*[← Index](00-introduction.md) · [Index](00-introduction.md) · [2 · Projects →](02-projects.md)*

---

Everything a camera runs — your scripts, settings, NPU models — ships inside a
**ROMFS image**: one file holding a read-only filesystem that the camera mounts at
`/rom`. This page is the tool for those images — `openmv-ota romfs`, the simplest verb
and the one underneath everything the later pages build.

## Why a read-only image

On most boards the flash holding the ROMFS is **memory-mapped**: the camera reads a
file from `/rom` in place, without copying it into RAM first. That is what lets a
multi-megabyte NPU model run on a board with a few hundred KB of heap — the NPU reads
the model blob straight out of flash. It is also why **byte alignment** matters: a
blob that is mapped, not copied, must start on the boundary its consumer requires.
The tool handles that for you — see [Alignment](#alignment) below.

## Packing a directory

`pack` writes the contents of a directory into an image — here a three-file app
(`main.py`, `settings.py`, `models/detector.tflite`), with the summary it prints:

```bash
$ openmv-ota romfs pack ./app -o app.romfs --board OPENMV_N6
  size:       3.09 KiB (3167 bytes)
  board:      OPENMV_N6 (OpenMV N6 (STM32N657))
  partition:  [0] ROMFS - capacity 24.00 MiB
  usage:      0.0%  (24.00 MiB free)
  alignment:  tflite=32, lite=32, onnx=32, bin=32
```

The directory's contents become the root of the image. Files are packed
unchanged. To compile `.py` files and convert NPU models automatically while
packing, use `openmv-ota build romfs` ([page 6](06-building.md)), which works
from a pegged project.

`--board` sets the alignment rules and partition capacity for a camera. Run
`openmv-ota romfs boards` for the list of board names, or pass one to see its
partitions and rules:

```bash
openmv-ota romfs boards
openmv-ota romfs boards OPENMV_AE3
```

The same directory always produces the same image; entries are packed in sorted
order.

### Alignment

Some files are mapped directly out of the image and must start on a specific
byte boundary — most importantly the model blobs read by the NPU. Each board
sets the required alignment per file extension; for example, the N6 aligns
`.tflite` to 32 bytes, while most boards use 16. `--board` applies these
automatically.

Use `--align` to set or override the alignment for an extension:

```bash
openmv-ota romfs pack ./app -o app.romfs --board OPENMV_N6 --align tflite=32
```

`--align` takes precedence over the board's rule for that extension. Extensions
with no rule use `--default-alignment`, which is 4 bytes.

### Options

| Flag | Effect |
|---|---|
| `-b, --board NAME` | Use a camera's alignment rules and partition capacity. |
| `-p, --partition N` | Select a partition on cameras that have more than one. |
| `--align EXT=N` | Set the alignment for a file extension (repeatable). Also spelled `--alignment`. |
| `--default-alignment N` | Alignment for extensions without a rule. Defaults to 4. |
| `--no-board-rules` | Ignore the board's alignment rules and use only `--align`. |
| `--exclude GLOB` | Skip entries whose name matches GLOB (repeatable). |
| `--no-default-excludes` | Pack `__pycache__`, `*.pyc`, `.git`, `.DS_Store`, and similar, which are skipped by default. |
| `--follow-symlinks` | Follow symlinks instead of skipping them. |
| `--max-size BYTES` | Capacity to check against. Defaults to the partition size. Accepts `0x…` and `K`/`M`/`G` suffixes. |
| `--allow-oversize` | Warn instead of failing when the image is larger than the capacity. |
| `-q, --quiet` | Do not print the summary. |

## Unpacking an image

`unpack` writes the image's contents to a directory:

```bash
openmv-ota romfs unpack app.romfs -o ./out
```

## Inspecting an image

```bash
$ openmv-ota romfs ls app.romfs -l       # contents, with sizes and offsets
          35  off=36        sfx=py     main.py
<dir>        models/
        3000  off=128       sfx=tflite models/detector.tflite
          15  off=3152      sfx=py     settings.py

$ openmv-ota romfs cat app.romfs main.py   # write one file to stdout
$ openmv-ota romfs inspect app.romfs       # summary
$ openmv-ota romfs verify app.romfs --board OPENMV_N6
```

Note the offsets: the model sits at **128** — the N6's rule says `tflite` aligns to
32 bytes, and the packer spent padding to make it so.

`verify` confirms the image parses and every file sits on its required boundary,
and exits non-zero on a malformed image or a misaligned file.

## Standard input and output

Pass `-` as the image path to read from standard input or write to standard
output:

```bash
openmv-ota romfs pack ./app -o - --board OPENMV_N6 | openmv-ota romfs verify -
```

## Where this goes next

Packing verbatim is the floor. A [project](02-projects.md) pegs your app to a
firmware so [`build romfs`](06-building.md) can compile it the way that firmware
expects — and for an over-the-air project, wrap this same image format in a signed
trailer that a camera will verify before it ever mounts it.

And if you want to know how the image is laid out on flash, byte by byte — say, to
write your own reader — that lives in
[the format anatomy reference](../reference/romfs-format.md).

---

*[← Index](00-introduction.md) · [Index](00-introduction.md) · [2 · Projects →](02-projects.md)*

# The ROMFS

*[← Index](00-introduction.md) · [Index](00-introduction.md) · [2 · Projects →](02-projects.md)*

---

Everything a camera runs — your scripts, settings, NPU models — ships inside a
**ROMFS image**: one file holding a read-only filesystem that the camera mounts at
`/rom`. This page is the tool for those images — `openmv-ota romfs`, the simplest verb,
and the one everything else is built on.

## Why a read-only image

On most boards the flash holding the ROMFS is **memory-mapped**: the camera reads a
file from `/rom` in place, without copying it into RAM first. That is what lets a
multi-megabyte NPU model run on a board with a few hundred KB of heap — the NPU reads
the model blob straight out of flash. It is also why **byte alignment** matters: a
blob that is mapped, not copied, must start on the boundary its consumer requires.
The tool handles that for you.

## Packing a directory

`pack` writes the contents of a directory into an image — here a three-file app
(`main.py`, `settings.py`, `models/detector.tflite`), with the summary it prints:

```bash
$ openmv-ota romfs pack ./app -o app.romfs --board OPENMV_N6
  size:       3.00 MiB (3145895 bytes)
  board:      OPENMV_N6 (OpenMV N6 (STM32N657))
  partition:  [0] ROMFS - capacity 24.00 MiB
  usage:      12.5%  (21.00 MiB free)
  alignment:  tflite=32, lite=32, onnx=32, bin=32
```

The directory's contents become the root of the image, packed exactly as they
are — `pack` never compiles a `.py` or converts a model.

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

It refuses a destination that is not empty; pass `--force` to unpack into one
anyway.

## Inspecting an image

### ls — list the contents

```bash
$ openmv-ota romfs ls app.romfs -l
          35  off=36        sfx=py     main.py
<dir>        models/
     3145728  off=128       sfx=tflite models/detector.tflite
          15  off=3145880   sfx=py     settings.py
```

`-l, --long` adds each file's size and offset. Note the offsets: the model sits at
**128** — the N6's rule says `tflite` aligns to 32 bytes, and the packer spent
padding to make it so.

### cat — read one file

```bash
$ openmv-ota romfs cat app.romfs main.py
import sensor
while True:
    pass
```

Takes the image and the path of the file inside it, and writes the file to stdout.

### inspect — summarise

```bash
$ openmv-ota romfs inspect app.romfs
  image size:   3.00 MiB (3145895 bytes)
  files:        3  (payload 3.00 MiB)
  directories:  1
  magic:        OK (D2 CD 31)
```

No options — one image in, one summary out.

### verify — check integrity and alignment

```bash
$ openmv-ota romfs verify app.romfs --board OPENMV_N6
OK: 3 files, 1 directory, all payloads aligned
```

`verify` confirms the image parses and every file sits on its required boundary,
and exits non-zero on a malformed image or a misaligned file — a CI gate. It takes
the same alignment options as `pack`, since checking is the mirror of packing:
`-b, --board` (whose rules to check against), `-p, --partition`, `--align EXT=N`
(override a rule, repeatable), `--default-alignment N`, and `--no-board-rules`.

## Standard input and output

Every command accepts `-` as the image path — read from standard input, or (for
`pack -o -`) write to standard output. That lets the tool compose with anything
else in a pipeline, with no temporary image file to create and clean up:

```bash
# gate a build in CI without leaving an artifact behind
openmv-ota romfs pack ./app -o - --board OPENMV_N6 | openmv-ota romfs verify -

# look inside an image you are fetching, without saving it first
curl -s https://builds.example.com/app.romfs | openmv-ota romfs ls - -l
```

## Digging deeper

If you want to know how the image is laid out on flash, byte by byte — say, to
write your own reader — that lives in
[the format anatomy reference](../reference/romfs-format.md).

---

*[← Index](00-introduction.md) · [Index](00-introduction.md) · [2 · Projects →](02-projects.md)*

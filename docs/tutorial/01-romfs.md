# The ROMFS

*[← Index](00-introduction.md) · [Index](00-introduction.md) · [2 · Projects →](02-projects.md)*

---

Everything a camera runs — your scripts, settings, NPU models — ships inside a
**ROMFS image**: one file holding a read-only filesystem that the camera mounts at
`/rom`. This page explains what that image is, how it is laid out on flash, and how to
drive `openmv-ota romfs`, the simplest verb and the tool underneath everything the
later pages build.

## Why a read-only image

On most boards the flash holding the ROMFS is **memory-mapped**: the camera reads a
file from `/rom` in place, without copying it into RAM first. That is what lets a
multi-megabyte NPU model run on a board with a few hundred KB of heap — the NPU reads
the model blob straight out of flash. It is also why **byte alignment** matters: a
blob that is mapped, not copied, must start on the boundary its consumer requires.
The format below is designed so alignment costs almost nothing.

## The image format

Three ideas, and the whole format falls out of them.

**1 — Variable-length integers.** Every number is base-128, big-endian: seven bits
per byte, and the high bit says "another byte follows". Small numbers cost one byte,
sizes are never fixed-width:

```
300 = 0b0000010_0101100  →  0x82 0x2C
          │        └─ low 7 bits, high bit clear (last byte)
          └─ next 7 bits, high bit set (continue)
```

**2 — Records.** The image is made of one shape only:

```
┌──────┬─────────────────┬──────┬────────────────────┐
│ kind │ padding (0x80…) │ size │ payload (size B)   │
└──────┴─────────────────┴──────┴────────────────────┘
```

| kind | meaning |
|---|---|
| 0 | unused / name carrier |
| 1 | padding |
| 2 | data, stored verbatim |
| 3 | data, stored as a pointer |
| 4 | directory |
| 5 | file |

**3 — The padding trick.** The `padding` run between `kind` and `size` is the
alignment mechanism, and it is free. Each `0x80` byte is a valid continuation byte
carrying seven **zero** bits — so the decoder just reads them as leading zeros of
`size`, no special case at all, while the writer inserts exactly enough of them that
the payload starts on the boundary it needs. Padding is parsing.

**Putting it together.** The whole image is a single header record whose `kind` is
the three magic bytes `D2 CD 31` — which are themselves `'R'|0x80, 'M'|0x80, '1'`, so
the magic is simultaneously a readable signature *and* a valid varint. Its payload,
aligned to 16, is the root directory's entries, concatenated:

```
image
└── header record        kind = D2 CD 31 ("RM1"), payload aligned to 16
    ├── file record      kind 5, aligned to 8
    │   ├── name         length-prefixed, e.g. 07 "main.py"
    │   └── data record  kind 2, payload aligned per the extension rule
    ├── directory record kind 4
    │   ├── name         e.g. 06 "models"
    │   └── …child file/directory records…
    └── …
```

A directory's payload is its name followed by its children — the same record shapes,
nested. A file's data record gets the **extension alignment rule** applied to its
payload, so the file's actual bytes (not its record header) land on the boundary.

## Reading a real image

Pack a small app and look at what came out — every byte below is genuine output:

```bash
$ openmv-ota romfs pack ./app -o app.romfs --board OPENMV_N6
  size:       3.09 KiB (3167 bytes)
  board:      OPENMV_N6 (OpenMV N6 (STM32N657))
  partition:  [0] ROMFS - capacity 24.00 MiB
  usage:      0.0%  (24.00 MiB free)
  alignment:  tflite=32, lite=32, onnx=32, bin=32

$ openmv-ota romfs ls app.romfs -l
          35  off=36        sfx=py     main.py
<dir>        models/
        3000  off=128       sfx=tflite models/detector.tflite
          15  off=3152      sfx=py     settings.py
```

Note the offsets: `main.py` sits at 36 (a 4-byte boundary, the default), while the
model landed at **128** — the N6's rule says `tflite` aligns to 32, and the writer
spent padding bytes to make it so. The first bytes of the image show all three ideas
at once:

```
00000000  d2 cd 31 80 80 80 80 80 80 80 80 80 80 80 98 4f
          └─magic─┘ └──padding: zero continuation bytes──┘ └size─┘
          "RM1"      absorbed as leading zeros of size…    = 3151
00000010  05 80 80 80 80 80 80 2f 07 6d 61 69 6e 2e 70 79
          │  └────padding─────┘ │  │  m  a  i  n  .  p  y
          └ kind 5: file    size=47 └ name, length-prefixed
00000020  02 80 80 23 69 6d 70 6f 72 74 20 73 65 6e 73 6f …
          │  └pad─┘ │  i  m  p  o  r  t     s  e  n  s  o
          └ kind 2: data  size=35 — and the payload starts at offset 36
```

The header's eleven `0x80` bytes exist only so the root payload starts at byte 16;
the decoder never knew they were there.

## Packing a directory

`pack` writes the contents of a directory into an image:

```bash
openmv-ota romfs pack ./app -o app.romfs --board OPENMV_N6
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
openmv-ota romfs ls app.romfs -l         # contents, with sizes and offsets
openmv-ota romfs cat app.romfs main.py   # write one file to stdout
openmv-ota romfs inspect app.romfs       # summary
openmv-ota romfs verify app.romfs --board OPENMV_N6
```

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

---

*[← Index](00-introduction.md) · [Index](00-introduction.md) · [2 · Projects →](02-projects.md)*

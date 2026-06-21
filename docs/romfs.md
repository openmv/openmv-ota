# romfs

`openmv-ota romfs` builds an OpenMV ROMFS image from a directory and unpacks one
back. A ROMFS image is the read-only filesystem the camera mounts at `/rom`.

## Building an image

`build` packs the contents of a directory into an image:

```bash
openmv-ota romfs build ./app -o app.romfs --board OPENMV_N6
```

The directory's contents become the root of the image. Files are stored
unchanged; compile `.py` files and convert models to their target format before
packing.

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
openmv-ota romfs build ./app -o app.romfs --board OPENMV_N6 --align tflite=32
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

`extract` writes the image's contents to a directory:

```bash
openmv-ota romfs extract app.romfs -o ./out
```

## Inspecting an image

```bash
openmv-ota romfs ls app.romfs -l         # contents, with sizes and offsets
openmv-ota romfs cat app.romfs main.py   # write one file to stdout
openmv-ota romfs info app.romfs          # summary
openmv-ota romfs verify app.romfs --board OPENMV_N6
```

`verify` confirms the image parses and every file sits on its required boundary,
and exits non-zero on a malformed image or a misaligned file.

## Standard input and output

Pass `-` as the image path to read from standard input or write to standard
output:

```bash
openmv-ota romfs build ./app -o - --board OPENMV_N6 | openmv-ota romfs verify -
```

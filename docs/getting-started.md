# Getting started

Install the tools:

```bash
pip install openmv-ota
```

## Pack a directory into a ROMFS image

The quickest path needs no firmware checkout. Pack a directory of files into a
ROMFS image for a board, then flash or inspect it:

```bash
openmv-ota romfs pack ./app -o app.romfs --board OPENMV_N6
openmv-ota romfs ls app.romfs -l
```

Files are packed as-is. See [romfs.md](romfs.md).

## Build from a project

To compile the app the way the firmware expects — `.py` to `.mpy`, and NPU models
for Vela / ST Edge AI — peg a project to a local OpenMV checkout, then build:

```bash
openmv-ota project new ./my-product -f ~/openmv -b OPENMV_N6
openmv-ota build romfs ./my-product
```

`project new` records the firmware commit and the exact toolchain versions it
implies; `build romfs` compiles `./my-product/app` and writes a ROMFS image to
`./my-product/build`. See [project.md](project.md) and [build.md](build.md).

Compiling needs mpy-cross. `openmv-ota project setup` installs the matching
version; if you skip setup, install it yourself with `pip install
mpy-cross==<version>` (the version `openmv-ota project show` reports).

Commit `openmv-ota.toml` and `openmv-ota.lock.json`. On another machine, run
`openmv-ota project setup` to reconstruct the pinned checkout and SDK before
building.

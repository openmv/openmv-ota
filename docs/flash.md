# Flashing a board

`openmv-ota flash` pushes the artifacts `build` produced onto a connected board over its
programming interface. Phase 1 drives **`dfu-util`**, which covers the OpenMV STM32 boards
(OPENMV2/3/4/4P/PT and N6); the Alif (AE3) and i.MX (RT1060) backends slot in behind the
same commands later.

Flash one board at a time — the device you have plugged in, named with `-b`:

```
openmv-ota flash firmware ./my-product -b OPENMV4
openmv-ota flash romfs    ./my-product -b OPENMV4
openmv-ota flash factory  ./my-product -b OPENMV4
```

| command | flashes | from |
| --- | --- | --- |
| `flash firmware` | the firmware image | `<board>-firmware.bin` |
| `flash romfs` | the app romfs image | `<board>-romfs.img` |
| `flash factory` | firmware **+** the dual-slot factory image (the manufacturing program) | `<board>-firmware.bin`, `<board>-factory-romfs.img` |

`flash factory` writes firmware and the factory image in one pass, leaving DFU only after
the final write so the device stays in the bootloader between steps. On the AE3, add
`--coprocessor` to `flash firmware`/`flash factory` to also flash the HE-core image (it's a
flag, not a separate command, because the coprocessor only exists on that board).

## Where the targets come from

Each board's `flash` block in `boards.json` is the source of truth: the `dfu` backend, the
`vid:pid`, and which DFU **alt-setting** each artifact lands on — e.g. OPENMV4 is
`37c5:9204` with firmware at alt 2 and romfs at alt 3. The alt is the index into the
board's bootloader partition table, so it isn't uniform across boards (N6 puts firmware at
alt 1); the map is explicit per board rather than assumed.

## Options

- `-o, --output DIR` — where the artifacts are (default `<project>/build`).
- `--dry-run` — print the `dfu-util` commands without running them. Useful to see the exact
  device id / alt / file before committing to a flash.
- `--no-leave` — don't reboot out of DFU after flashing (stay in the bootloader).
- `--dfu-util PATH` — use a specific `dfu-util` (default: the SDK's `bin/dfu-util` when
  `--sdk-home` is given, else one on `PATH`).
- `--sdk-home DIR` — find `dfu-util` under `<DIR>/bin`.

```
$ openmv-ota flash factory ./my-product -b OPENMV4 --dry-run
would run: dfu-util -d 37c5:9204 -a 2 -D build/OPENMV4-firmware.bin
would run: dfu-util -d 37c5:9204 -a 3 -D build/OPENMV4-factory-romfs.img -s :leave
```

A board with no `flash` block (RT1060 today) or a not-yet-supported backend fails with a
clear message rather than guessing.

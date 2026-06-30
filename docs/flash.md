# Flashing a board

`openmv-ota flash` pushes the artifacts `build` produced onto a connected board over its
programming interface. Phase 1 drives **`dfu-util`**, which covers every DFU board: the
OpenMV STM32 boards (OPENMV2/3/4/4P/PT and N6) **and the AE3** — the AE3 flashes its two
cores, its coprocessor romfs, and its main romfs all over DFU (the Alif write-mram tool is
only for programming its bootloader). The i.MX (RT1060) backend, and CubeProgrammer for the
N6's initial factory-bootloader burn, slot in later.

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

`flash factory` writes firmware and the factory image in one pass, resetting the board only
after the final write so it stays in the bootloader between steps.

The **AE3** is dual-core, and its HE core ships *with* the firmware — the two core images
can't be flashed separately. So there's no flag: `flash firmware` always writes both cores
(HP + HE), and `flash factory` always writes all four partitions (alts 1/2/3/6 = HP fw, HE
fw, coprocessor romfs, main romfs). If either core image is missing the flash fails fast
rather than half-programming.

## Where the targets come from

Each board's `flash` block in `boards.json` is the source of truth: the `dfu` backend, the
`vid:pid`, which DFU **alt-setting** each artifact lands on, and (where it differs) the
artifact's filename — e.g. OPENMV4 is `37c5:9204` with firmware at alt 2 and romfs at alt 3,
while the AE3 is `37c5:96e3` with its firmware at alt 1 named `firmware-M55_HP.bin`. The alt
is the index into the board's bootloader partition table, so it isn't uniform across boards
(N6 puts firmware at alt 1); the map is explicit per board, taken from the OpenMV IDE's
`settings.json`.

The `dfu-util` invocation mirrors the IDE's: `-w` (wait for the device to re-enumerate),
`-d ,<vid:pid>` (match it in DFU mode), and `--reset` on the final step so the board reboots
only after the last write.

## Options

- `-o, --output DIR` — where the artifacts are (default `<project>/build`).
- `--dry-run` — print the `dfu-util` commands without running them. Useful to see the exact
  device id / alt / file before committing to a flash.
- `--no-reset` — don't reset (reboot) the board after flashing (stay in the bootloader).
- `--dfu-util PATH` — use a specific `dfu-util` (default: the SDK's `bin/dfu-util` when
  `--sdk-home` is given, else one on `PATH`).
- `--sdk-home DIR` — find `dfu-util` under `<DIR>/bin`.

```
$ openmv-ota flash factory ./my-product -b OPENMV4 --dry-run
would run: dfu-util -w -d ,37c5:9204 -a 2 -D build/OPENMV4-firmware.bin
would run: dfu-util -w -d ,37c5:9204 -a 3 --reset -D build/OPENMV4-factory-romfs.img
```

A board with no `flash` block, or a not-yet-supported backend, fails with a clear message
rather than guessing.

## i.MX RT1060

The RT1062 has no resident DFU bootloader — it flashes through the ROM's serial-download
protocol with NXP's `sdphost` + `blhost` (the SDK's spsdk tools, found under `--sdk-home`'s
`python/bin`). The same `flash firmware` / `flash romfs` / `flash factory` verbs apply; the
backend just runs a longer sequence: `sdphost` loads a RAM flashloader and jumps to it, then
— after the flashloader re-enumerates (the tool settles and polls `blhost get-property` until
it answers) — `blhost` configures the FlexSPI NOR and writes each region. `flash factory`
does the full provision (flash-config block, secure bootloader, firmware, romfs, and the boot
e-fuse); `flash firmware`/`flash romfs` rewrite just that one region. It works the same as any
other board — nothing extra to supply:

```
$ openmv-ota flash factory ./my-product -b OPENMV_RT1060 --dry-run
would run: sdphost -u 0x1FC9,0x0135 -- write-file 0x20001C00 .../sdphost_flash_loader.bin
would run: sdphost -u 0x1FC9,0x0135 -- jump-address 0x20001C00
would run: blhost -u 0x15A2,0x0073 -- get-property 1
...
would run: blhost -u 0x15A2,0x0073 -- reset
```

The two flashloader binaries the sequence needs (`sdphost_flash_loader.bin`,
`blhost_flash_loader.bin`) are an internal detail — prebuilt copies ship inside the tool, so
you never supply or carry them. This whole backend is temporary: the RT1062 will move to the
same DFU bootloader as the other cameras, and when it does this path (and those bundled files)
goes away.

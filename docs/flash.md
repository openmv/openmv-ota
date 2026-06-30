# Flashing a board

`openmv-ota flash` pushes the artifacts `build` produced onto a connected board over its
programming interface. Most boards use **`dfu-util`**: the OpenMV STM32 boards
(OPENMV2/3/4/4P/PT and N6) **and the AE3** — the AE3 flashes its two cores, its coprocessor
romfs, and its main romfs all over DFU (the Alif write-mram tool is only for programming its
bootloader). The **Arduino** boards (Portenta H7, Giga, Nicla Vision) also use dfu-util, by
address (see below). The **RT1060** uses its own `sdphost`/`blhost` backend (see below). The
only piece still to come is CubeProgrammer for the N6's one-time factory-bootloader burn.

Flash one board at a time — the device you have plugged in, named with `-b`:

```
openmv-ota flash firmware ./my-product -b OPENMV4
openmv-ota flash romfs    ./my-product -b OPENMV4
openmv-ota flash factory  ./my-product -b OPENMV4
```

## Getting into the bootloader

You don't have to put the camera in its bootloader first — `flash` does it. It scans the
serial ports for a board running its firmware (by the board's runtime USB VID:PID), resets it
into the bootloader (**OpenMV** boards via `machine.bootloader()` over `mpremote`; **Arduino**
boards via a 1200-baud touch), and captures the board's USB serial number so the flash is
pinned to that exact device (`dfu-util -S`) when several are attached.

If **several** of the same board are connected, pass `--serial <SN>` to pick one. If the board
is **already** in its bootloader (no serial port present), it's flashed as-is — pass
`--in-bootloader` to skip the detect/reset step entirely. `--mpremote` overrides how mpremote
is invoked (default `python -m mpremote`).

| command | flashes | from |
| --- | --- | --- |
| `flash firmware` | the firmware image | `<board>-firmware.bin` |
| `flash romfs` | the app romfs image | `<board>-romfs.img` |
| `flash factory` | firmware **+** the dual-slot factory image (the manufacturing program) | `<board>-firmware.bin`, `<board>-factory-romfs.img` |
| `flash bootloader` | the bootloader (see below) | `<board>-bootloader.bin` |

`flash factory` writes firmware and the factory image in one pass, resetting the board only
after the final write so it stays in the bootloader between steps.

The **AE3** is dual-core, and its HE core ships *with* the firmware — the two core images
can't be flashed separately. So there's no flag: `flash firmware` always writes both cores
(HP + HE), and `flash factory` always writes all four partitions (alts 1/2/3/6 = HP fw, HE
fw, coprocessor romfs, main romfs). If either core image is missing the flash fails fast
rather than half-programming.

## What each board does

The OpenMV DFU boards differ only in their `vid:pid` and which alt-setting each artifact
lands on:

| Board | backend | vid:pid | firmware | romfs | notes |
| --- | --- | --- | --- | --- | --- |
| OPENMV2 | dfu (alt) | 37c5:9202 | alt 2 | alt 3 | |
| OPENMV3 | dfu (alt) | 37c5:9203 | alt 2 | alt 3 | |
| OPENMV4 | dfu (alt) | 37c5:9204 | alt 2 | alt 3 | |
| OPENMV4P | dfu (alt) | 37c5:924a | alt 2 | alt 4 | |
| OPENMVPT | dfu (alt) | 37c5:9205 | alt 2 | alt 4 | |
| OPENMV_N6 | dfu (alt) | 37c5:9206 | alt **1** | alt 3 | firmware before filesystem |
| OPENMV_AE3 | dfu (alt) | 37c5:96e3 | alt 1 (HP) | alt 6 | + HE fw alt 2, coprocessor romfs alt 3 |
| ARDUINO_PORTENTA_H7 | dfu (addr) | 2341:035b | 0x08040000 | 0x90B00000 | + CYW4343 wifi blobs; touch-to-reset |
| ARDUINO_GIGA | dfu (addr) | 2341:0366 | 0x08040000 | 0x90B00000 | + CYW4343 wifi blobs; touch-to-reset |
| ARDUINO_NICLA_VISION | dfu (addr) | 2341:035f | 0x08040000 | 0x90B00000 | + CYW4343 wifi blobs; touch-to-reset |
| OPENMV_RT1060 | imx | sdphost/blhost | 0x60040000 | 0x60800000 | full sequence (below) |

The OpenMV boards address partitions by **alt-setting**; the Arduino boards by **address**
(`-a <alt> -s 0xADDR`); the RT1060 has its own `sdphost`/`blhost` backend. A single-partition
write (`flash firmware`, `flash romfs`) is one `dfu-util` call; a multi-partition write
resets only on the final step so the board stays in the bootloader between them. For example,
on **OPENMV4**:

```
# flash firmware
dfu-util -w -d ,37c5:9204 -a 2 --reset -D OPENMV4-firmware.bin

# flash factory  (firmware, then the dual-slot factory image)
dfu-util -w -d ,37c5:9204 -a 2 -D OPENMV4-firmware.bin
dfu-util -w -d ,37c5:9204 -a 3 --reset -D OPENMV4-factory-romfs.img
```

and the **AE3** `flash factory`, all four partitions in order:

```
dfu-util -w -d ,37c5:96e3 -a 1 -D OPENMV_AE3-firmware-M55_HP.bin
dfu-util -w -d ,37c5:96e3 -a 2 -D OPENMV_AE3-firmware-M55_HE.bin
dfu-util -w -d ,37c5:96e3 -a 3 -D OPENMV_AE3-coprocessor-romfs.img
dfu-util -w -d ,37c5:96e3 -a 6 --reset -D OPENMV_AE3-factory-romfs.img
```

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
- `--in-bootloader` — the board is already in its bootloader; skip the detect/reset step.
- `--serial SN` — USB serial number of the camera to flash (when several are attached).
- `--mpremote PATH` — how to run mpremote (default `python -m mpremote`).
- `--dfu-util PATH` — use a specific `dfu-util` (default: the SDK's `bin/dfu-util` when
  `--sdk-home` is given, else one on `PATH`).
- `--sdk-home DIR` — find `dfu-util` under `<DIR>/bin`.

```
$ openmv-ota flash factory ./my-product -b OPENMV4 --dry-run
would run: dfu-util -w -d ,37c5:9204 -a 2 -D build/OPENMV4-firmware.bin
would run: dfu-util -w -d ,37c5:9204 -a 3 --reset -D build/OPENMV4-factory-romfs.img
```

A board with no `flash` block, or a not-yet-supported backend, fails with a clear message
rather than guessing. A **retired** board (the Nano RP2040 Connect and Nano 33 BLE Sense —
the firmware builds but crashes at boot, and they have no ROMFS for OTA) is refused by *every*
tool — `flash`, `build`, and `project` — with the same "no longer supported" message.

## Arduino boards (Portenta H7, Giga, Nicla Vision)

These run the Arduino MCUboot DFU bootloader, so dfu-util addresses flash by absolute
**address** (`-a <alt> -s 0xADDR`) and leaves via `-s 0xADDR:leave` rather than `--reset`.
The verbs are the same; `flash firmware` writes the app at `0x08040000`, `flash romfs` the
romfs at QSPI `0x90B00000`, and `flash factory` additionally writes the **CYW4343** wifi/bt
firmware to QSPI — a full first-time provision. Those wifi blobs version-track the firmware,
so `build firmware` copies them out of the firmware tree (`drivers/cyw4343/firmware/`) into
the output dir alongside the image — pinned to the exact firmware you just built. `flash
factory` reads them from there like any other artifact; you never supply them, and a stale
copy can never be written. Writes erase-on-write, so there's no separate erase pass.

To flash, the board must be in its DFU bootloader. If it's in app mode the tool
**touch-to-resets** it — opens its serial port at 1200 baud, which the bootloader detects and
reboots into DFU — then `dfu-util -w` waits for it. If you'd rather double-tap reset yourself,
pass `--no-touch`.

```
$ openmv-ota flash factory ./my-product -b ARDUINO_PORTENTA_H7 --dry-run
would run: dfu-util -w -d ,2341:035b -a 1 -s 0x90F00000 -D .../cyw4343_7_45_98_102.bin
would run: dfu-util -w -d ,2341:035b -a 1 -s 0x90FC0000 -D .../cyw4343_btfw.bin
would run: dfu-util -w -d ,2341:035b -a 0 -s 0x08040000 -D ARDUINO_PORTENTA_H7-firmware.bin
would run: dfu-util -w -d ,2341:035b -a 1 -s 0x90B00000:leave -D ARDUINO_PORTENTA_H7-romfs.img
```

## Flashing the bootloader

`flash bootloader` writes the board's `<board>-bootloader.bin` (collected by `build firmware`).
It's a different path from firmware/romfs: those reach the board through the *OpenMV*
bootloader (which we reset into automatically), but the bootloader can only be written from
the board's **system ROM DFU**, which you enter **by hand** (jumper BOOT→RST / BOOT0/REC→3.3V,
then replug). So there's no auto-reset — `flash bootloader` prints the board's instructions and
waits for the system-DFU device, then writes to `0x08000000` via `dfu-util`. (dfu-util may
report a nonzero exit on the final status — expected for the ST ROM; the tool tolerates it.)

```
$ openmv-ota flash bootloader -b OPENMV4
BOOT0 is sampled at power-on, so the jumper must be on BEFORE the board is powered: unplug
the camera first, jumper BOOT0 (the REC pad) to 3.3V, then plug it back in with the jumper
held. Wait for the system DFU bootloader to enumerate; remove the jumper after flashing.
```

Supported on the OpenMV STM32 boards (OPENMV2/3/4/4P/PT). The others report what to do instead:
the **N6** uses STM32CubeProgrammer + a FlashLayout.tsv (not yet wired); the **RT1060**'s secure
bootloader is written by `flash factory`; the **AE3** uses Alif SE tools; **Arduino** boards
have no OpenMV bootloader to flash.

## i.MX RT1060

The RT1062 has no resident DFU bootloader — it flashes through the ROM's serial-download
protocol with NXP's `sdphost` + `blhost` (the SDK's spsdk tools, found under `--sdk-home`'s
`python/bin`). The same `flash firmware` / `flash romfs` / `flash factory` verbs apply; the
backend just runs a longer sequence: `sdphost` loads a RAM flashloader and jumps to it, then
— after the flashloader re-enumerates (a single process waits for it by polling spsdk's USB
scan in-process, the `dfu-util -w` equivalent, rather than relaunching `blhost` to retry) —
`blhost` configures the FlexSPI NOR and writes each region. `flash factory`
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

## Typical use

```
# Manufacturing — provision a fresh board (firmware + the golden factory image):
openmv-ota build firmware       -b OPENMV4
openmv-ota build factory-romfs  -b OPENMV4
openmv-ota flash factory        -b OPENMV4

# Iterate on the app image on a dev board:
openmv-ota build romfs  -b OPENMV4
openmv-ota flash romfs  -b OPENMV4

# See exactly what would run before committing to a flash:
openmv-ota flash factory -b OPENMV4 --dry-run
```

On a multi-core board (the AE3) the same commands flash every partition the image spans —
`build` produces the per-core firmware and the coprocessor romfs in lockstep with the main
image, and `flash` writes them together. Flashing always resolves every artifact first, so a
missing file fails fast instead of half-programming the board.

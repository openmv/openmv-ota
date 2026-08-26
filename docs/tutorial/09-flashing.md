# Flashing

*[← 8 · Release artifacts](08-release-artifacts.md) · [Index](00-introduction.md) · [10 · Bootloader & erase →](10-bootloader-and-erase.md)*

---

`openmv-ota flash` pushes the artifacts `build` produced onto a connected board,
picking the right programming backend per board. Every verb covers every
supported board. Flash one board at a time — the device you have plugged in,
named with `-b`:

```
openmv-ota flash firmware ./my-product -b OPENMV4
openmv-ota flash romfs    ./my-product -b OPENMV4
openmv-ota flash factory  ./my-product -b OPENMV4
```

| command | flashes | from |
| --- | --- | --- |
| `flash firmware` | the firmware image | `<board>-firmware.bin` |
| `flash romfs` | the app romfs image | `<board>-romfs.img` |
| `flash factory` | firmware **+** the factory image (the manufacturing program) | `<board>-firmware.bin`, `<board>-factory-romfs.img` |
| `flash bootloader` | the bootloader — [next page](10-bootloader-and-erase.md) | `<board>-bootloader.bin` |
| `flash erase` | the onboard filesystem — [next page](10-bootloader-and-erase.md) | — (no artifact) |
| `flash list` | *(query)* connected boards + the state each is in | — |

A multi-partition write (`flash factory`) resets the board only after the final
write, so it stays in the bootloader between steps — and every flash resolves
its artifacts first, so a missing file fails fast instead of half-programming
the board.

## Getting into the bootloader

You don't have to put the camera in its bootloader first — `flash` does it. It
finds a board running its firmware, resets it into the bootloader (OpenMV
boards via `machine.bootloader()`; Arduino boards via a 1200-baud touch), and
pins the flash to that exact device by USB serial number when several are
attached. If several of the same board are connected, pass `--serial <SN>` to
pick one; if the board is already in its bootloader, it's flashed as-is — pass
`--in-bootloader` to skip the detect/reset step entirely.

## What each board uses

Each board's `flash` block in `boards.json` is the source of truth; `--dry-run`
prints the exact commands for yours.

| Board | backend | vid:pid | firmware | romfs | notes |
| --- | --- | --- | --- | --- | --- |
| OPENMV2 | dfu (alt) | 37c5:9202 | alt 2 | alt 3 | |
| OPENMV3 | dfu (alt) | 37c5:9203 | alt 2 | alt 3 | |
| OPENMV4 | dfu (alt) | 37c5:9204 | alt 2 | alt 3 | |
| OPENMV4P | dfu (alt) | 37c5:924a | alt 2 | alt 4 | |
| OPENMVPT | dfu (alt) | 37c5:9205 | alt 2 | alt 4 | |
| OPENMV_N6 | dfu (alt) | 37c5:9206 | alt 1 | alt 3 | |
| OPENMV_AE3 | dfu (alt) | 37c5:96e3 | alt 1 (HP) | alt 6 | + HE fw alt 2, coprocessor romfs alt 3 — cores never flash separately: `firmware` writes both, `factory` all four |
| ARDUINO_PORTENTA_H7 | dfu (addr) | 2341:035b | 0x08040000 | 0x90B00000 | + CYW4343 wifi/bt blobs (collected by `build firmware`); 1200-baud touch-to-reset |
| ARDUINO_GIGA | dfu (addr) | 2341:0366 | 0x08040000 | 0x90B00000 | + CYW4343 wifi/bt blobs (collected by `build firmware`); 1200-baud touch-to-reset |
| ARDUINO_NICLA_VISION | dfu (addr) | 2341:035f | 0x08040000 | 0x90B00000 | + CYW4343 wifi/bt blobs (collected by `build firmware`); 1200-baud touch-to-reset |
| OPENMV_RT1060 | imx | sdphost/blhost | 0x60040000 | 0x60800000 | SDP/flashloader sequence via the SDK's tools (`--sdk-home`); temporary until it gets the DFU bootloader |

On an alt-addressed board, each partition is one `dfu-util` call:

```
$ openmv-ota flash factory ./my-product -b OPENMV4 --dry-run
would run: dfu-util -w -d ,37c5:9204 -a 2 -D build/OPENMV4-firmware.bin
would run: dfu-util -w -d ,37c5:9204 -a 3 --reset -D build/OPENMV4-factory-romfs.img
```

An Arduino board flashes by address, wifi/bt blobs first:

```
$ openmv-ota flash factory ./my-product -b ARDUINO_PORTENTA_H7 --dry-run
would run: dfu-util -w -d ,2341:035b -a 1 -s 0x90F00000 -D .../cyw4343_7_45_98_102.bin
would run: dfu-util -w -d ,2341:035b -a 1 -s 0x90FC0000 -D .../cyw4343_btfw.bin
would run: dfu-util -w -d ,2341:035b -a 0 -s 0x08040000 -D ARDUINO_PORTENTA_H7-firmware.bin
would run: dfu-util -w -d ,2341:035b -a 1 -s 0x90B00000:leave -D ARDUINO_PORTENTA_H7-romfs.img
```

And the RT1060 runs its longer serial-download sequence:

```
$ openmv-ota flash factory ./my-product -b OPENMV_RT1060 --dry-run
would run: sdphost -u 0x1FC9,0x0135 -- write-file 0x20001C00 .../sdphost_flash_loader.bin
would run: sdphost -u 0x1FC9,0x0135 -- jump-address 0x20001C00
...
would run: blhost -u 0x15A2,0x0073 -- reset
```

## Options

| Flag | Effect |
| --- | --- |
| `-o, --output DIR` | Where the artifacts are (default `<project>/build`). |
| `--dry-run` | Print the exact commands without running them. |
| `--no-reset` | Don't reboot the board after flashing (stay in the bootloader). |
| `--in-bootloader` | The board is already in its bootloader; skip the detect/reset step. |
| `--serial SN` | USB serial number of the camera to flash (when several are attached). |
| `--mpremote PATH` | How to run mpremote (default `python -m mpremote`). |
| `--dfu-util PATH` | Use a specific `dfu-util` (default: the SDK's when `--sdk-home` is given, else `PATH`). |
| `--sdk-home DIR` | Find the SDK's flashing tools (`dfu-util`, `blhost`). |

## Listing connected boards

`flash list` enumerates every connected board it can identify and the state
each is in — which board is which, what to pass `--serial`, did it actually
enter the bootloader:

```
$ openmv-ota flash list
OPENMV4              running    /dev/ttyACM0       208237AD3548
OPENMV_AE3           recovery   /dev/ttyUSB0        -
OpenMV STM32         recovery   system DFU         3648335A3138
OPENMV_RT1060        recovery   system flashloader  -
```

State is **`running`** (firmware up), **`bootloader`** (the normal DFU the
flash verbs use), or **`recovery`** (the by-hand ROM/maintenance modes you
enter to flash a *bootloader* — [next page](10-bootloader-and-erase.md)). A
board that looks identical to others in a shared ROM mode is reported under a
generic label, and each scanner degrades on its own when its tool is missing.
`--json` prints the same as a machine-readable array.

## Typical use

```
# Manufacturing — provision a fresh board (firmware + the factory image):
openmv-ota build firmware       -b OPENMV4
openmv-ota build factory-romfs  -b OPENMV4
openmv-ota flash factory        -b OPENMV4

# Iterate on the app image on a dev board:
openmv-ota build romfs  -b OPENMV4
openmv-ota flash romfs  -b OPENMV4

# See exactly what would run before committing to a flash:
openmv-ota flash factory -b OPENMV4 --dry-run
```

---

*[← 8 · Release artifacts](08-release-artifacts.md) · [Index](00-introduction.md) · [10 · Bootloader & erase →](10-bootloader-and-erase.md)*

# Erase & bootloader

*[← 9 · Flashing](09-flashing.md) · [Index](00-introduction.md) · [11 · Boot and rollback →](11-boot-and-rollback.md)*

---

Two maintenance verbs ride the same `flash` tool: **erasing** the onboard
filesystem, and writing the **bootloader** itself. Both are occasional
operations — nothing here is part of a normal build-flash-iterate loop.

## flash erase

`flash erase` wipes the board's **user disk** — the same "Erase Onboard Data
Flash" the IDE does. It invalidates the filesystem partition and the firmware
reformats a clean disk on the next boot, entering and leaving the bootloader
the same way the flash verbs do; the disk is its own partition, so firmware
and romfs are untouched:

| Board | erase target |
| --- | --- |
| OPENMV2 / 3 / 4 | alt 1 |
| OPENMV4P / OPENMVPT | alt 3 |
| OPENMV_N6 | alt 2 |
| OPENMV_AE3 | alt 5 (its RWFS) |
| ARDUINO_PORTENTA_H7 / GIGA | app at `0x08020000` + QSPI filesystem at `0x90000000` |
| ARDUINO_NICLA_VISION | QSPI filesystem only |
| OPENMV_RT1060 | the disk's MBR sector at `0x60400000`, via blhost |

```
$ openmv-ota flash erase -b OPENMV4 --dry-run
would run: dfu-util -w -d ,37c5:9204 -a 1 --reset -D <4KB-zeros>

$ openmv-ota flash erase -b OPENMV_RT1060 --dry-run
would run: sdphost -u 0x1FC9,0x0135 -- write-file 0x20001C00 .../sdphost_flash_loader.bin
...
would run: blhost -u 0x15A2,0x0073 -t 120000 -- flash-erase-region 0x60400000 0x1000
would run: blhost -u 0x15A2,0x0073 -- reset
```

## flash bootloader

`flash bootloader` writes the board's `<board>-bootloader.bin` (collected by
`build firmware`). It's a different path from firmware/romfs: those reach the
board *through* the OpenMV bootloader, but the bootloader itself can only be
written from the board's ROM **recovery mode**, which you enter by hand — the
tool prints the board's instructions and waits:

```
$ openmv-ota flash bootloader -b OPENMV4
BOOT0 is sampled at power-on. On an already-programmed camera, force it into system DFU:
unplug the camera, jumper BOOT0 to 3.3V (VCC), plug it back in with the jumper held, then
remove the jumper after flashing. A virgin (unprogrammed) camera comes up in system DFU on
its own -- no jumper needed. Wait for the system DFU bootloader to enumerate.
```

| Board | recovery entry | written via |
| --- | --- | --- |
| OPENMV2 / 3 / 4 / 4P / PT | BOOT0 jumpered to 3.3V, replug (virgin boards come up in recovery on their own) | system DFU |
| OPENMV_N6 | BOOT0 jumpered to 3.3V, replug | STM32CubeProgrammer (the layout + loader files it needs are bundled in the tool) |
| OPENMV_RT1060 | SBL jumper — keep it on until flashing finishes | SDP/blhost (no build artifact: writes the flash-config block + the bundled secure bootloader) |
| OPENMV_AE3 | SE-UART maintenance mode, over the board's USB-serial bridge (not the OpenMV USB port) | Alif Security Toolkit (see below) |
| Arduino boards | — | no OpenMV bootloader to flash |

### The AE3's Security Toolkit flow

The AE3's bootloader lives in MRAM and is written with Alif's tools, vendored
in the openmv firmware tree (`tools/alif/toolkit` — run `git submodule update
--init` once). `flash bootloader` finds the SE-UART port and picks the matching
part automatically, then:

1. updates the **system package** (the SE firmware is coupled to the bootloader);
2. asks you to **unplug and replug** the board, and re-finds the port;
3. writes the bootloader and TOC to MRAM.

Afterwards the AE3 re-enumerates as the OpenMV DFU bootloader — run
`flash firmware` to write the application. Recovering a board that won't enter
maintenance mode is left to the IDE.


---

*[← 9 · Flashing](09-flashing.md) · [Index](00-introduction.md) · [11 · Boot and rollback →](11-boot-and-rollback.md)*

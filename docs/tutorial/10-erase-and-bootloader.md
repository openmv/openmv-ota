# Erase & bootloader

*[← 9 · Flashing](09-flashing.md) · [Index](00-introduction.md) · [11 · Boot and rollback →](11-boot-and-rollback.md)*

---

Three maintenance verbs ride the same `flash` tool: **erasing** the onboard
filesystem, **reading** a partition back, and writing the **bootloader** itself.
All are occasional operations — nothing here is part of a normal
build-flash-iterate loop.

## flash erase

`flash erase` wipes the board's onboard filesystem — the writable disk at
`/flash`, the same "Erase Onboard Data Flash" the IDE does. In an OTA product
the app ships read-only in the romfs, so what lives here is runtime data:
config, logs, whatever your app wrote. The erase invalidates the partition and
the firmware reformats a clean disk on the next boot, entering and leaving the
bootloader the same way the flash verbs do; the filesystem is its own
partition, so firmware and romfs are untouched:

| Board | erase target |
| --- | --- |
| OPENMV2 / 3 / 4 | alt 1 |
| OPENMV4P / OPENMVPT | alt 3 |
| OPENMV_N6 | alt 2 |
| OPENMV_AE3 | alt 5 (its RWFS) |
| ARDUINO_PORTENTA_H7 / GIGA / NICLA_VISION | QSPI filesystem at `0x90000000` |
| OPENMV_RT1060 | the disk's MBR sector at `0x60400000`, via blhost |

```
$ openmv-ota flash erase -b OPENMV4 --dry-run
would run: dfu-util -d ,37c5:9204 -a 1 --reset -D <zeros>

$ openmv-ota flash erase -b OPENMV_RT1060 --dry-run
would run: blhost -u 0x15A2,0x0073 -t 120000 -- flash-erase-region 0x60400000 0x1000
would run: blhost -u 0x15A2,0x0073 -- reset
```

## flash read

`flash read` copies a partition out of the flash part into a file, through the
board's resident bootloader, and writes nothing. The bootloader runs before
anything that switches the chip's flash cipher on, so on a camera that keeps
its flash encrypted the file holds the bytes as they are stored — what someone
reading the flash part would get — and not what the running firmware sees.
`--part firmware` is the whole firmware area, `--part romfs` both app slots.
The camera is reset into the bootloader and runs again afterwards.

It is for the OPENMV_RT1060, which has no DFU bootloader; on a DFU board
`dfu-util -U` uploads a partition the same way.

```
$ openmv-ota flash read -b OPENMV_RT1060 --part firmware --to rt-read-firmware.bin
Reading memory
Response status = 0 (0x0) Success.
Response word 1 = 3932160 (0x3c0000)
Read 3932160 of 3932160 bytes.
Response status = 0 (0x0) Success.
read firmware 0x60040000 (0x3C0000) -> rt-read-firmware.bin (OPENMV_RT1060)
reset (OPENMV_RT1060)
```

The firmware area takes about 17 s to read, the two slots (8 MiB) about 44 s.

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
| OPENMV_N6 | BOOT0 jumpered to 3.3V, replug (virgin boards come up in recovery on their own) | STM32CubeProgrammer (the layout + loader files it needs are bundled in the tool) |
| OPENMV_RT1060 | SBL jumper — keep it on until flashing finishes | SDP/blhost (no build artifact: writes the flash-config block + the bundled secure bootloader) |
| OPENMV_AE3 | SE-UART maintenance mode, over the board's USB-serial bridge (not the OpenMV USB port) | Alif Security Toolkit |
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
`flash firmware` to write the application. If the bootloader flash fails,
recover the board with the OpenMV IDE instead.


---

*[← 9 · Flashing](09-flashing.md) · [Index](00-introduction.md) · [11 · Boot and rollback →](11-boot-and-rollback.md)*

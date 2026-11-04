# Code protection: encrypted off-chip flash — survey and design questions

**Status: DRAFT (2026-11-03).** This is the groundwork for the next phase. Nothing here is
decided; the "Decisions to make" section is what the owner settles before any code is
written. Facts marked *(verify)* were taken from vendor headers in the firmware tree or
from memory and need the reference manual before they carry a design.

## Context

Every OTA board keeps something that matters in a flash chip outside the MCU package: the
app ROMFS on all of them, the firmware itself on the N6 and the RT1062. A chip on the board
can be desoldered and read, or its bus sniffed, so nothing in it is confidential today. The
goal of this phase is that the bytes at rest in off-chip flash are ciphertext, and that the
MCU decrypts them *on the fly* as it executes or reads them -- with the hardware engine
where the part has one, so performance and memory-mapped (XIP, `/rom`) use are unchanged.

What already exists, and stays:

- **Transport encryption** ([payload keys](../tutorial/05-signing-keys.md)): images travel
  AES-256-CBC under a board key baked into the firmware. That protects the download, not the
  flash.
- **Authenticity**: root → signing certificate → image signature, and the stub/`boot.py`
  verify before anything is installed. Encryption is an addition, never a replacement.
- **The stub** (`firmware-updates.md`): the one piece of our code that runs before the
  firmware on every boot and already owns the pre-boot flash work. Its design note always
  said it would host full-drive encryption.

## What a board can and cannot protect

| Board | MCU | Firmware lives in | App ROMFS lives in | On-the-fly engine | Key store | Debug lock |
|---|---|---|---|---|---|---|
| OPENMV_N6 | STM32N657 | external XSPI flash, XIP at 0x70000000 | same chip (ROMFS0 at +8 MiB) | **MCE** (Memory Cipher Engine): 4 regions per instance, AES-128/256 or Noekeon; *stream* mode = read-only decrypt, *block* / *fast block* modes allow **read-write** (writes encrypted on the way in) *(verify: RM0486)* | BSEC OTP words; HAL `stm32n6xx_hal_bsec.c` is in the tree | BSEC lifecycle / secure-boot closed state *(verify)* |
| OPENMV_RT1060 | i.MX RT1062 | external FlexSPI NOR, XIP at 0x60000000 | same chip | **BEE** (Bus Encryption Engine): 2 regions, AES-128 CTR or ECB, decrypt-only on the XIP read path | OCOTP fuses: SW_GP2 or the OTPMK-derived key; `fsl_bee.c` / `fsl_ocotp.c` are in the tree | HAB closed + JTAG fuse (irreversible) |
| OPENMV_AE3 | Alif Ensemble E3 | **on-die MRAM** (0x80000000) | external OSPI flash (ROMFS0 in the 32 MiB part) | MRAM needs none (it is inside the package). OSPI: Alif's controller has an AES XIP decryption block *(verify: Alif OSPI/AES docs)* | Alif Secure Enclave / OTP *(verify)* | SE lifecycle: debug disable *(verify)* |
| OPENMV4P, OPENMVPT | STM32H743 | internal flash (0x08060000 behind the stub) | external QSPI (ROMFS at +24 MiB) | **none** -- the H743/H747 have no OTFDEC (the engine exists on H7A3/B3 only; `lib/stm32/h7/*otfdec*` is for those) | internal flash RDP; no OTP key slot for an engine | RDP level 1 (reversible by mass erase) / level 2 (permanent) |
| ARDUINO_PORTENTA_H7, ARDUINO_GIGA | STM32H747 | internal flash | external QSPI | none (as above) | as above | as above; the Arduino bootloader is not ours |
| ARDUINO_NICLA_VISION | STM32H747 | internal flash | external QSPI | none; and **no SDRAM**, so the software fallback below is not available either | as above | as above |
| OPENMV2/3/4 (classic, app-only) | F4/F7/H7 | internal flash | internal flash / SD | not an OTA firmware board; RDP covers the firmware | RDP | RDP |

So the phase splits by hardware, not by product line:

1. **N6 and RT1062**: the whole image -- firmware and app -- can be ciphertext at rest and
   decrypted in hardware with no runtime cost. This is where "on-the-fly decryption" is
   literally available, and where the firmware itself is exposed today.
2. **AE3**: the firmware is already unreadable from outside (MRAM is on-die); only the app
   ROMFS on the OSPI part is exposed. Whether the OSPI controller decrypts XIP in hardware
   decides whether this board joins group 1 for the app or group 3.
3. **STM32H743/H747 boards**: the firmware is in internal flash and RDP already protects it
   (level 2 permanently). The app ROMFS on the QSPI part can only be protected in
   *software*: decrypt the active slot into SDRAM at boot and mount `/rom` from RAM (the
   slots are 1-2 MiB; AES at tens of MB/s on an M7 is well under a second), which the Nicla
   cannot do (no SDRAM). Nothing makes the QSPI *writes* of an install confidential-at-rest
   without a key the firmware holds in software, which RDP then protects.

## How an install would have to work with an engine in the path

The engine decrypts what the core *reads* from a region. Everything we *write* into that
region must therefore already be ciphertext under the engine's key and counter scheme:

- **Stub installing firmware** (N6, RT1062): the stub decompresses the signed blob and
  writes the firmware. Today it writes plaintext. With an engine, it must encrypt each block
  as it programs it -- which means the stub needs the device key in software (OTP words it
  can read), or the engine must offer a write path. The N6 MCE's read-write block modes
  *(verify)* would let the stub write plaintext through the memory-mapped window and have
  the engine encrypt it; the RT1062 BEE is read-only, so the stub does AES-CTR itself with
  the key it reads from OCOTP before HAB closes that off *(verify which fuse words stay
  readable to software after the device is closed)*.
- **Installer writing a ROMFS slot** (all boards): `installer.py` streams the downloaded
  image into the slot through the block device. The same question: encrypt in Python (a C
  helper -- `cryptolib` AES-CTR with the engine's address-derived counter), or write through
  a read-write engine region. Delta installs reconstruct blocks from the *old* slot, which
  the engine decrypts on read -- that path keeps working as long as reads go through the
  region.
- **`boot.py` and the trailer**: hashes and signatures are over the plaintext image (what
  the engine presents on read), so verification is unchanged -- provided the engine region
  is configured *before* the stub reads the trailers. On the N6 and RT1062 that is the
  bootloader's job (ours on the N6; the RT1062's SBL is ours too), or the stub's own first
  act with the key it reads.
- **A/B slots and the key**: both slots and the firmware area share one device key, so a
  key is per device, not per release. A release stays one artifact: the device encrypts
  what it stores. Per-device server-side encryption is not needed and is not wanted (one
  download per fleet, not per camera).

## Keys

- **Per device, generated on the device, never leaves it.** Burned once into OTP/fuses
  (N6 BSEC, RT1062 OCOTP, Alif SE) at `flash factory` time or on first boot of a factory
  image, by the stub or the bootloader. The host never knows it; the fleet needs nothing
  new server-side. This is the same stance as the device identity keys.
- **One-way**: fuses do not unburn. A fused dev board is fused. The HIL fleet and the bench
  boards need an explicit decision (below) before any key is burned anywhere.
- **Debug lock is part of the design, not an option**: an engine whose key is readable over
  SWD protects nothing. Closing the device (RDP2, HAB closed, BSEC closed, SE debug off) is
  irreversible on every part. Recovery of a closed board is DFU/ROM-boot only, with signed
  images -- which the N6 and RT1062 already assume.

## Decisions to make (owner)

1. **Scope of this phase.** Group 1 only (N6 + RT1062, hardware engines, firmware and app
   both encrypted)? Or also the H7 software fallback for the app ROMFS (SDRAM boards), which
   is a different mechanism with a runtime cost and no cover for the Nicla?
2. **Who sets up the engine before the stub reads flash**: the bootloader (reads the OTP key
   and configures the regions, never hands the key on) or the stub (configures the engine
   itself, keeps the key for its own encrypt-on-install)? The RT1062's BEE is read-only, so
   something must encrypt on write with the key in software there regardless.
3. **Key provisioning moment and tooling**: `flash factory` (host-driven, over USB, before
   the board leaves the bench) vs. first boot of a factory image (device-driven, no host
   step). Either way: what `openmv-ota` prints, records and refuses (a board already fused
   with a different key is a brick for any other product).
4. **Fleet and bench policy**: which boards get fused and closed. A closed board cannot be
   re-flashed over SWD, so the HIL recovery paths (`recover.py --swd-loader`, J-Link
   revives) stop working on it. Proposal: one fused N6 and one fused RT1062 as permanent
   "production-like" legs; everything else stays open, with the engine configured but the
   debug lock *not* applied (encryption at rest still exercised; the key is just readable).
5. **Which cipher on the N6**: AES-128 CTR stream (fastest decrypt, read-only) or a block
   mode that also encrypts writes (saves the stub a software AES). *(verify the throughput
   and the counter/nonce scheme for each in RM0486 before choosing.)*
6. **Upgrade path**: cameras in the field today hold plaintext. An encrypted image on a
   board whose engine is off does not boot; an engine on a board whose flash is plaintext
   does not boot either. The switch is a factory flash, not an OTA -- unless the stub learns
   to encrypt-in-place on its first boot with a key (one-time, power-loss-safe?). Decide
   whether in-place conversion is in scope at all.

## Open questions to verify against documentation

- STM32N657: MCE instances per XSPI (MCE1 → XSPI1, MCE2 → XSPI2?), region granularity and
  alignment, counter derivation in stream mode, whether block modes allow executable XIP at
  full speed, how the key is loaded (direct registers vs. BSEC/SAES-wrapped), what the boot
  ROM requires of the FSBL for a *closed* device (signed FSBL header -- our bootloader is
  that FSBL).
- i.MX RT1062: BEE region alignment, CTR nonce scheme (the BEE "PRDB" protection region
  descriptor block the ROM parses when booting an encrypted image), which key source
  (SW_GP2 vs OTPMK) leaves a software-readable key for encrypt-on-install, and how our SBL
  at 0x60000000 interacts with "encrypted XIP" boot.
- Alif E3: does the OSPI controller decrypt XIP in hardware, and is that usable for the
  ROMFS mounted by MicroPython's `VfsRom` over the memory map?
- STM32H743/H747: RDP2 interaction with the Arduino bootloader (not ours) and with DFU
  recovery; whether "decrypt the active ROMFS slot into SDRAM" fits the boards' SDRAM
  budget next to the frame buffers.

## Not in scope

- Encrypting `/flash` (the user filesystem). It is not load-bearing and holds no code.
- Protecting against an attacker with the device open and SWD available: that is the
  debug lock's job, and it is binary.
- Server-side per-device encryption of releases (see "A/B slots and the key").

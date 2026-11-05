# Code protection: encrypted off-chip flash — survey and design questions

**Status: N6 phase settled (2026-11-04).** The survey below covers every OTA board; the
"Decisions" section records what the owner settled for the first phase (the N6 only) and
"Verified on the N6" what was checked against RM0486 and on the bench. Rows for the other
boards still carry *(verify)* marks: they are groundwork for later phases, not design.

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
| OPENMV_N6 | STM32N657 | external XSPI flash, XIP at 0x70000000 | same chip (ROMFS0 at +8 MiB) | **MCE** (Memory Cipher Engine), MCE2 on XSPI2: 4 regions of 4 KiB granularity, AES-128/256 or Noekeon; *stream*, *block* and *fast block* modes all cipher reads **and** writes | none needed: a key derived from the ST-fused hardware unique key (HUK) through the SAES, see "Keys" | BSEC life cycle: shipped CLOSED_UNLOCKED (debug open); `secure_boot` fuse + debug lock = CLOSED_LOCKED, one-way |
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
  as it programs it. On the N6 the stub holds the derived key in software anyway (it has to
  load it into the MCE), so it can run AES-CTR with the MCE's address-derived counter and
  program ciphertext through the normal indirect-mode path -- or write plaintext through the
  memory-mapped window and let the stream-mode engine encrypt it (16-byte bursts); which is
  faster is a bench measurement. The RT1062 BEE is read-only, so the stub does AES-CTR
  itself with the key it reads from OCOTP before HAB closes that off *(verify which fuse
  words stay readable to software after the device is closed)*.
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

- **Per device, derived from the chip's own hardware key, stored nowhere.** ST fuses a
  256-bit hardware unique key (HUK) into every N6 at the factory; software cannot read it,
  only the SAES can use it. The stub has the SAES encrypt a fixed constant under the
  HUK-derived key (`KEYSEL` = DHUK, normal mode, output readable) and loads the result into
  the MCE's write-only key registers. The same key comes out on every boot, the host never
  knows it, nothing is burned and nothing is provisioned. `key_store.c` already uses this
  exact path to seal the device identity keys. (RT1062 and Alif: the survey rows stand;
  nothing is settled for them.)
- **Fuses are deferred, deliberately.** Encryption at rest is on by default from the first
  N6 build of this phase, so every bench and HIL board runs the real data path from day one.
  What it does *not* buy yet is protection against a probe: until the device is closed and
  debug is locked, code with SWD access can derive the same key. Closing (`secure_boot`
  fuse, debug lock, the public-key hash for the bootloader) is a separate, later lockdown
  step that changes nothing about how the firmware behaves -- which is the point of
  ordering it this way.
- **The derivation is level-bound.** The DHUK depends on the privilege, security state,
  hide-protection level (HDPL) and SAES mode of the code deriving it. The stub derives it at
  the bootloader's level (HDPL 1, where the firmware runs today too), the same every boot.

## Decisions (settled with the owner, 2026-11-04)

1. **Scope: the N6 only.** Hardware engine (MCE), firmware and app ROMFS both ciphertext at
   rest. The RT1062 follows once the N6 path is proven; the H7 software fallback and the AE3
   OSPI question stay in the survey.
2. **The stub owns the engine.** It derives the key, programs the MCE regions before it reads
   anything from the slots, and encrypts what it installs. The bootloader is unchanged.
3. **No key provisioning step.** The key is derived from the HUK (see "Keys"). `flash factory`
   burns nothing and records nothing new.
4. **No board is fused in this phase.** Every N6 stays as shipped (BSEC closed, `secure_boot`
   unblown, debug open), so `recover.py`, J-Link revives and DFU keep working. Encryption at
   rest is exercised on all of them regardless. Fused "production-like" legs come with
   lockdown, not before.
5. **Cipher: AES-128 in the MCE's stream mode on MCE2**, the instance in front of XSPI2 (the
   flash at 0x70000000) and what ST's own encrypted-XIP example uses. Stream mode ciphers
   reads and writes and has the XSPI prefetch optimisation; it offers no bit-flip protection,
   which the trailer hashes already provide. Throughput is confirmed on the bench before the
   first install path is written.
6. **No in-place conversion, no field upgrade from plaintext.** Nothing is in the field; the
   switch is a factory flash. Factory flow (to prove on the bench): DFU writes the stub and a
   plaintext factory slot; the stub's first boot verifies that slot raw, installs the firmware
   from it (encrypting as it programs), copies it into the other slot as ciphertext through
   the normal install path, marks it, then erases the plaintext one. A power cut leaves
   either a verifying plaintext slot or a verifying encrypted one, never neither.
7. **The stub image carries a signature from now on**, so a future bootloader can verify it
   under the project's root without a stub change (the stub is flashed over USB only and its
   formats are permanent). The format goes into `firmware-updates.md` with the stub build;
   the bootloader side is lockdown work.

Still open, and a bench measurement rather than a decision: how the installer (Python)
encrypts a slot it writes -- AES-CTR in `cryptolib` with the MCE's address-derived counter,
one call per 4 KiB chunk, or a C helper.

## Verified on the N6 (RM0486, ST wiki and UM3234, bench 2026-11-04)

- **Secure boot is a signature, not encryption.** The boot ROM checks an ECDSA (P-256 or
  P-384) signature over the FSBL header plus image. The header carries the signing public key
  and a table of up to 8 key hashes; the hash of that table must match OTP words 160-167
  (`OTP_ROT_HASH`). Revocation: OTP 17. Anti-rollback counters: OTP 20-21. FSBL *encryption*
  is optional (OTP 18 bit 4, key derived from OTP 364-367) and not wanted: the FSBL is our
  bootloader, public code.
- **Life cycle.** Parts ship BSEC-closed with `secure_boot` (OTP 18 bits 3:0) unblown, ST's
  "CLOSED_UNLOCKED": the ROM checks the signature but boots on failure, and debug is open.
  Blowing `secure_boot` ("CLOSED_LOCKED") enforces it. Our bootloader is already built with a
  boot-ROM header (`STM32_SigningTool_CLI -nk`, header 2.3); signing it later is the same
  tool with a key. Read on the HIL N6, read-only: `NVSTATE` 0x0D (closed), `HVALID` 1,
  `HDPLSR` 0x51 (HDPL 1), OTP 18 = 0, OTP 160-167 = 0, `DBGCR` 0xB4B4B400 and `AP_UNLOCK`
  0xB4 (debug opened by the ROM).
- **HUK and DHUK.** 256-bit HUK fused by ST (OTP 376-383), wired to the SAES only. `KEYSEL`
  0b001 = DHUK, 0b010 = BHK (battery-backed; our boards have no battery), 0b100 = both.
  Usable only BSEC-closed (`HVALID`); unusable BSEC-open, zeroed on tamper. The SAES can hand
  a key to CRYP over its key bus but not to the MCE: software carries the derived key across.
- **MCE.** MCE2 fronts XSPI2 (0x70000000). Four regions, 4 KiB granularity, inclusive end
  address, ciphered regions may not overlap. Stream, block and fast block modes all cipher
  reads and writes; stream has the XSPI prefetch optimisation and no integrity. Keys are
  AES-128/256 or Noekeon in write-only registers that read back as zero, lockable until MCE
  reset (`MKLOCK`, `KEYLOCK`, `GLOCK`), erased on tamper. Stream counter: `NR1 || NR0 ||
  16 zero bits || VERSION[15:0] || 0000 || ADDR[31:4]`, so a writer produces matching
  ciphertext with plain AES-CTR and an IV built from the nonce, the version and the
  destination address. The XSPI must be in memory-mapped mode; DMA writes through it go in
  16-byte bursts.
- **OTP.** Words 0-127 bitwise, 128-255 word-once, 256-375 secret and word-once; customer
  ranges 40-55, 59-95, 173-255 and 260-363. Nothing in OTP is reversible. The RM0486
  chapters were read from an unofficial HTML mirror: spot-check word numbers against the PDF
  before any fuse is ever touched.

## Open questions to verify against documentation

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

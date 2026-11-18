# Code protection: encrypted off-chip flash — survey and design questions

**Status: N6 phase settled (2026-11-04); the N6 firmware area built (2026-11-05).** The
survey below covers every OTA board; the "Decisions" section records what the owner settled
for the first phase (the N6 only), "Verified on the N6" what was checked against RM0486 and
on the bench, and "The N6 firmware area, as built" the first piece that exists. Rows for the
other boards still carry *(verify)* marks: they are groundwork for later phases, not design.

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
  writes the firmware, so it must encrypt each block as it programs it. On the N6 the stub
  holds the derived key in software anyway (it has to load it into the MCE), so it runs the
  MCE's stream cipher itself and programs ciphertext through the normal indirect-mode path,
  where the engine is not involved. The RT1062 BEE is read-only, so the stub does AES-CTR
  itself with a key it derives in the DCP from fuses software cannot read (see "The RT1062,
  as built").
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
  exact path to seal the device identity keys. (The RT1062's key is in "The RT1062, as
  built"; nothing is settled for Alif.)
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
   formats are permanent). The format is the footer in
   [firmware-updates.md](firmware-updates.md#stub-image-footer), written by every stub
   build since 2026-11-08 and signed by the factory key; the bootloader side is lockdown
   work.

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
  reset (`MKLOCK`, `KEYLOCK`, `GLOCK`), erased on tamper. The XSPI must be in memory-mapped
  mode; DMA writes through it go in 16-byte bursts. The stream counter's fields are in the
  manual; their byte order, and everything else a writer needs to match the engine, came off
  the bench.
- **OTP.** Words 0-127 bitwise, 128-255 word-once, 256-375 secret and word-once; customer
  ranges 40-55, 59-95, 173-255 and 260-363. Nothing in OTP is reversible. The RM0486
  chapters were read from an unofficial HTML mirror: spot-check word numbers against the PDF
  before any fuse is ever touched.

## Measured on the N6 (bench, OPENMV_N6, 2026-11-04)

Register pokes from MicroPython on a fleet board, over a 64 KiB scratch block in the ROMFS
slot that was not running (written raw through the ROMFS ioctls, read back through the memory
map with the data cache invalidated by address). Each line is something the design now rests
on; the stub's host tests hold the cipher to these vectors.

- **The derived key is stable.** A value derived under the DHUK came out identical before and
  after a hard reset, with the hide-protection level at 1 both times.
- **The stream cipher, exactly.** For the 16-byte block at AXI address `addr`:

      counter   = CCNR1 || CCNR0 || 0x0000 || VERSION || (addr >> 4 & 0x0FFFFFFF)   every field big-endian
      keystream = the bytes of AES-128(key, counter), reversed
      key       = BE(CCKEYR3) || BE(CCKEYR2) || BE(CCKEYR1) || BE(CCKEYR0)

  Flash holding zeros read back as that keystream on every block checked (the first three of
  the region and one 4 KiB in), reproduced with the host's AES. `addr` is the full
  address (0x70xxxxxx), not an offset into the region.
- **Writing `CCCFGR.MODE` zeroes the context's key.** Silently: the region still ciphers, with
  the keystream of an all-zero key, and `KEYCRC` reads 0. Set the mode first, then the nonce
  and the key, and check `KEYCRC` before enabling the context: it reads the value ST's
  `HAL_MCE_KeyCRCComputation` gives (0x95 for key words 00010203 04050607 08090a0b 0c0d0e0f).
  The HAL's own `HAL_MCE_ConfigAESContext` writes the key before the mode.
- **`CCKEYR0..3` must be written in that order.** In reverse the key stays invalid and the
  region reads as stored (bypass).
- **`SADDR`/`EADDR` read back without the top nibble** (0x00800000 for 0x70800000); the region
  matches the full address regardless.
- **Decryption costs nothing measurable.** 64 KiB through the memory map: 1.07 ms with the
  region on, 1.07 ms with it off, cold cache both times.
- **A deep sleep resets the engine.** After `machine.deepsleep()` the MCE2 clock was off and
  every register zero. The OpenMV firmware links the port's resume-from-retained-RAM code in
  flash (`iram_bootloader_reset` at 0x7011ff7c in firmware.map), where it cannot run before
  the flash is mapped. A firmware that did resume from RAM would have to enter the stub, which
  is plaintext, before its own reset handler, which is not.

## The N6 firmware area, as built (2026-11-05)

The firmware the stub installs is ciphertext at rest. The app's ROMFS slots are not yet, and
each slot still carries the same firmware as a compressed component in the clear: on its own
this piece hides nothing from someone who reads the whole chip. It is the key, the engine
set-up and the install path, proven on hardware, that the slots then use.

- **What is encrypted.** The stub's install window, 0x70090000-0x703FF000: MCE2 region 1 with
  context 1, AES-128 stream. The stub (0x70080000) and the install record (0x703FF000) stay
  in the clear: the bootloader has to run the one, and the stub has to read the other before
  it knows the tweak.
- **The key.** 16 bytes: the fixed label `omv-flash-key-v1`, AES-256-ECB encrypted in the SAES
  under the DHUK. The stub derives it on every boot before it reads the record, and stores it
  nowhere. If `HVALID` is clear the stub stops (`no hardware key`, blinking as it does when
  nothing boots), because the SAES would silently use a fixed key instead.
- **The tweak.** 80 bits, loaded as `CCNR1`, `CCNR0` and `VERSION`: the first 10 bytes of
  SHA-256 over the label `omv-fw-crypt-v1\0` and the install record's component entries
  (destination, size, installed hash). It is derived, not stored, so the record's format is
  unchanged. Two different installs never share a keystream; the same firmware installed twice
  gives the same ciphertext; an install cut off by a reset picks up where it stopped.
- **A boot with nothing to install.** The stub reads the record, programs the engine with its
  tweak, hashes the area through the memory map and, when it matches, locks the engine
  (`KEYLOCK`, `CCLOCK`, `GLOCK`) and jumps. Read from the running firmware: `CR` 0x00000011,
  `REGCR1` 0x00004201, `SADDR1` 0x00090000, `EADDR1` 0x003FEFFF; writes to an unused region
  and context were ignored.
- **An install.** Programming goes through the flash controller's indirect mode, where the
  engine is not in the path, so the stub applies the same keystream in software
  (`mce_stream.c`, held to the bench vectors by the host tests): to compare what a block
  already holds and to write what it should. Then it erases every whole block of the window
  that no component touches and that is not blank, writes the record, maps the flash again
  and hashes the area through the engine before it jumps. An area that does not hash is
  logged and the other slot is tried.
- **Firmware downloaded over USB.** The bootloader writes what it is given, so `flash factory`
  and `flash firmware` leave the firmware in the clear. On the next boot the area does not
  hash under the cipher but does hash as stored; the stub logs `firmware area in the clear:
  encrypting it` and installs the slot's copy as an ordinary install. With no slot that
  verifies, the stub still boots firmware that is there in the clear, with the engine off.
- **Deep sleep.** A wake is a reset: it comes back through the bootloader and the stub, which
  programs the engine again.

Measured on a fleet OPENMV_N6 (2,270,360-byte firmware, timestamps from the debug UART):

| Boot | Time in the stub |
|---|---|
| Nothing to install (key, engine, hash of the area) | under 0.5 s |
| Trial install, firmware unchanged (blocks compared, window checked for stale data) | 2.5 s |
| Install after a USB download (every block erased and programmed) | 13.5 s to 15 s |

- **The chip holds ciphertext.** The FIRMWARE partition read back over DFU (the bootloader
  runs before the stub, so nothing deciphers it): the stub's bytes equal the stub image; of
  the firmware's 141,898 16-byte blocks, none equal the build's; 8.000 bits of entropy per
  byte against the build's 7.355; the record's `OMVI` magic readable.
- **Why the window is scrubbed.** The first read-back found 276 KiB of an older, longer
  firmware past the end of the new one, in the clear. After the scrub everything between the
  firmware and the record is blank, bar the 8 bytes of padding that round the firmware up to
  the flash's 16-byte write unit.
- **Deep sleep, on the board.** After `machine.deepsleep(3000)` the debug UART showed the
  stub's boot, `flash cipher on`, `firmware area verified`, `boot normal`, and the app
  reporting `reset_cause=4`.
- **Update cycles.** `delta` (twice) and `bad_sig` from `ci/hil/ota_cycle.py` passed on the
  board with the encrypting stub.

The fleet repeats the read-back on every run (`ci/hil/ota_cycle.py`, `check_at_rest`): after
the `delta` scenario on a board whose stub encrypts, the harness enters the bootloader, uploads
the FIRMWARE partition over DFU and fails the scenario unless the stub's sector is the stub, no
16-byte block of the firmware is stored as built, no sample of the build appears anywhere in
the area, the stored firmware has more than 7.9 bits of entropy per byte, and everything after
it up to the record is blank. On the N6 node the read takes about 10 s and reported 0 of
141,897 blocks stored as built at 8.000 bits per byte.

The `fw_corrupt` scenario damages the area from outside: garbage over the first block of the
installed firmware, written through the bootloader, the record left alone. The next boot logs
`firmware area does not match the record`, installs the slot's copy again (about 3 s: one
block rewritten, the rest compared) and logs `boot reinstalled`.

Two more scenarios reach what is left of the stub's repair paths. `blob_corrupt` first takes an
update, so that both slots hold an image that verifies, and then also erases one block of the
booted slot's firmware blob, from the camera: the stub logs `blob hash mismatch` and `install
failed` for that slot, before anything is erased, and installs the other slot's copy (about
3.5 s). The damaged slot still verifies by its trailer and its image, so every later boot tries
it first and logs the same two lines before it boots the other slot. `no_slot`
erases both slots' first and trailer blocks from the camera and downloads the firmware over
USB, in the clear: both slots are rejected, no record vouches for a plaintext area, and the
stub boots what looks like firmware with the engine off (`no slot: booting the installed
firmware`). `no_slot_secure` is the same brick with the stub built as on a chip that holds a
secure boot key (`[ota].secure_boot = "always"`): the stub logs `secure boot: no verified
slot, nothing boots` and stops, and the golden flash over USB that ends the scenario brings
the camera back.

## The ROMFS slots, ciphertext at rest (2026-11-07)

What is still in the clear on the N6 after the firmware area: the two slots, each a filesystem
image, the firmware as a compressed component, a status sector and a trailer. They carry the app
and a copy of the firmware, so they are the piece that hides the product.

- **Engine budget.** MCE2 has four regions and two contexts. Region 1 with context 1 stays on
  the firmware area. Region 2 with context 2 covers the slot the stub boots, from the slot's
  first byte to the end of its last component, rounded up to the engine's 4 KiB region grain
  (the installer writes ciphertext up to that same bound, blank bytes included). The other slot is never mapped: the only code
  that reads it is the stub, verifying it, and the installer, writing it and reading it back,
  and both apply the keystream themselves. The engine's counter is the address, so what the
  engine deciphers through the memory map and what software deciphers from an indirect read
  are the same bytes (`mce_stream.c` already does this for the firmware area).
- **In the clear, in every slot:** the 0xFF pad after the last component, the status sector and
  the trailer. The stub and `boot.py` read the trailer before anything is keyed, the marks are
  programmed without an erase, and nothing in those 8 KiB is secret.
- **The tweak, per slot:** the first 10 bytes of SHA-256 over the label `omv-slot-crypt-1`,
  the slot index (a little-endian word), and the SHA-256 of the trailer's signed region. The slot index keeps the two
  slots apart even when they hold the same release; the trailer hash changes with every release,
  so a slot rewritten with a new image never reuses the keystream the old image had at the same
  addresses (the body hash alone would not do: two releases with the same app and a different
  firmware share it, and their firmware blobs would XOR together). The same device key as the
  firmware area; the labels keep the nonces apart.
- **The manifest names the tweak.** It gains the trailer's signed-region hash. A streamed image
  delivers its trailer last, and the installer must know the keystream before the first body
  byte is written; the manifest is signed, so the installer trusts the hash the way it trusts
  the body hash. The stub derives the same value from the trailer in flash, so an installer
  that keyed a slot wrongly produces a slot whose body does not hash, and the trial is refused.
- **The stub's boot.** Both trailers read raw; certificates and signatures checked as today.
  For each slot that passes, context 2 is keyed with that slot's tweak and the body hashed
  through the memory map; a body that does not hash is read raw and hashed again, which is how
  a factory slot is recognised. The choice is made as today. Context 2 stays keyed for the
  chosen slot, the region on, and the lock (both contexts, then `GLOCK`) is taken before the
  jump as it is now. During an install the stub reads the chosen slot's blobs in indirect mode
  through the slot's stream, as it reads the firmware area through its own.
- **Factory.** `flash factory` writes the stub, the firmware and the factory slot in the clear.
  On the first boot the stub verifies the slot raw, installs the firmware (encrypting it, as it
  does today for a USB-written area), then copies the slot into the other slot through the
  other slot's stream, status sector and trailer verbatim (confirmed, counter, floors), and
  verifies the copy through the engine. Only then is the raw slot erased, its trailer block
  first. The rule is general: a slot that verifies raw is converted into the other slot and
  then erased when no ciphered slot verifies (the factory flow, and a second factory run over
  DFU), and simply erased when one does (a conversion a reset cut short of its last step, or an
  installer that wrote plaintext: nothing in the clear ever boots, and nothing in the clear
  stays behind). A power cut anywhere repeats a step that compares before it writes. Cost on an
  N6: about 10 s to copy a slot and 6 s to erase the clear one, on top of the firmware install.
- **The installer (firmware).** A C module dropped into the firmware build the way
  `ecdsa_verify.c` is: it derives the device key once (SAES under the DHUK, the stub's label),
  shares the stream code with the stub, and XORs a buffer in place for a given flash address and
  tweak. The installer XORs each 4 KiB chunk before `write_at`, XORs the read-back before it
  compares, and XORs what it reads when it hashes the finished slot; the delta base is the
  running slot, mapped, so it is read in the clear as today. One chunk at a time, within the
  RAM budget. A firmware without the module writes in the clear, as every other board does.
- **`boot.py`.** Unchanged. It follows the install record to the mapped slot and reads it in
  the clear; the other slot's body does not hash for it, which it already tolerates (a slot it
  did not choose is allowed to be anything).
- **Stock firmware, the IDE.** As today: a slot the stub cannot verify is not mapped, and the
  installed firmware boots with the slots in the clear.
- **The fleet's read-back** extends to the ROMFS partition: after `delta` on the N6, the
  harness uploads it over DFU and expects, in both slots, no 16-byte block of the slot as the
  tool built it, more than 7.9 bits of entropy per byte over the body, the status sector and
  trailer readable, and the pad blank.

Measured on the fleet's OPENMV_N6 (bench, 2026-11-07; timestamps from the debug UART):

| Step | Time |
|---|---|
| Factory slot copied into the other slot through the cipher (2.5 MB used) | 10.5 s |
| The clear slot erased | 5.5 s |
| Firmware installed from the ciphered slot (first boot) | 14.5 s |
| A delta update written through `flash_crypt` (12 MB slot, 1.3 MB written) | 7 s |
| The trial boot's firmware install (blocks compared, one rewritten) | 3 s |

The first build of the conversion erased the blob's tail: the pad scrub started at the ciphered
range's end rounded *down* to an erase block, inside the last block the copy had written. The
stub's own blob hash caught it on the next boot (`blob hash mismatch`), before anything was
installed from the copy.

## The RT1062, as built (2026-11-14)

The i.MX RT1062 gets the N6's design: the firmware area and the booted slot are ciphertext at
rest and deciphered by the chip as it reads them; the stub and the installer encrypt what they
write. What differs is the engine, and where the key comes from.

**The key is the camera's own, in fuses it made itself.** NXP fuses a master key into every
part, but the chip releases it only once it is closed (HAB): on an open part, which is every
camera as shipped, a key derived from it is derived from sixteen zero bytes, the same on
every board. The first build of this design used that key, and an open RT1062's flash was
ciphertext that a flash dump and this repository decrypted. The key now comes from the chip's
SW_GP2 fuses instead: 128 bits of customer fuses that the DCP can take as its hardware key,
with a write lock and a read lock. The stub fills them on the first boot that finds them
unlocked, so the key is different on every camera, stays with the chip, and is not readable
by software, the chip open or closed. Closing the chip later does
not change it.

What an open chip still gives away is use, not the key: code running on the chip, or a
debugger on its open debug port, can ask the DCP to encrypt under the key and so derive the
flash key the way the stub does. Reading the flash part, the thing this phase is about, gets
ciphertext.

- **The engine (BEE).** It deciphers reads of the FlexSPI memory map, in the address ranges
  `IOMUXC_GPR` names, and does nothing for a write. Two regions, each with its own key and
  nonce: region 1 is a window (`REGION1_BOT`..`REGION1_TOP`), region 0 everywhere else. The
  firmware area (0x60050000..0x603FF000) reads through region 0, the booted slot's body through
  region 1, under one key and two tweaks. Outside those two ranges flash reads as it is stored:
  the stub, the install record, `/flash`, the other slot, and every slot's status sector and
  trailer.
- **The cipher, measured.** AES-128 in counter mode. The counter is the three high nonce words,
  big-endian, then the flash address shifted right by four:
  `BE32(W3) || BE32(W2) || BE32(W1) || BE32(addr >> 4)`. The key the engine uses is the bytes
  written to its key registers, reversed. The low nonce word is left at zero: its bits do not
  add to the counter, they rearrange the address field, and nothing documents how. The 80-bit
  tweak is the first ten counter bytes. Six reads of erased flash through the engine, under
  three nonces, match this model (`tests/build/test_bee_stream_c.py` holds the stub's C to
  them).
- **Who may read.** Each region has a security level, and it is an access filter, not a cipher
  setting. At level 3, the vendor driver's default, only the core's own reads are deciphered:
  another bus master reading a ciphered range gets a bus error (the DCP reading the firmware
  area failed with a source error; the engine's status showed a read violation). At levels 2,
  1 and 0 the same read returned plaintext. The firmware hands flash to bus masters as a
  matter of course -- the Wi-Fi chip's firmware is a constant in the image, sent to the radio
  by the SDIO controller's DMA straight from flash -- and the first build, at level 3, could
  not start its radio. The stub sets level 0: the engine deciphers for every master.
- **Reads that are not 16-byte aligned.** NXP's errata for the chip (ERR050164) say the
  engine supports 16-byte bursts only, that a master sending an unaligned burst may get a bus
  error, and that the way round it is aligned addresses and lengths, and for the core, the
  cache on. Measured on the fleet's board with both ranges on (2026-11-16), reading a ciphered
  range at each of the sixteen offsets of a block and comparing with the core's cached read:

  | Who reads | What was read | Result |
  |---|---|---|
  | The core, through a non-cacheable MPU region | 1 to 64 bytes at every offset; 8, 16 and 32-bit loads | all correct |
  | The SD controller's DMA (a file written to the card straight from flash) | 512, 4096, 5000 and 65536 bytes at every offset | all correct |
  | The DCP (memory copy) | 16, 20, 32, 61 and 64 bytes at every offset | correct, but for 16 bytes at offset 8: a bus error, 25 times of 25 |
  | The DCP (memory copy) | 1, 3 and 4 bytes at every offset | correct at offsets 0 to 12; a bus error at 13, 14 and 15, 25 times of 25 |

  The erratum is real, it is the same every time, and it is a bus error, never wrong data.
  Nothing the firmware does meets it: the stub's hand-over with the caches off reads through
  the core, the Wi-Fi chip's firmware (at 0x6023AD64 in the fleet's build, offset 4) and SD
  writes go through the SD controller, and neither the stub nor the firmware points the DCP at
  flash. `machine.SPI` copies through the core. A driver that hands flash to another DMA
  master is the thing to test first.
- **Three things the bench showed.** A range's top register takes the range's END address
  (end minus 8 leaves the last 16 bytes as stored). The data cache keeps what it read before a
  range changed, so the stub drops it on every change. And the DCP's status reads zero for a
  moment after its clock is switched on, so the stub waits for the fuse key to show.
- **The key.** One AES-128-ECB block of the label `omv-flash-key-v1` in the DCP under its
  hardware key (key select 0xFF), with the DCP's key input pointed at the SW_GP2 fuses
  (`IOMUXC_GPR10` bit 4) and the DCP reset so it takes them. Derived on every boot, stored
  nowhere, never handed to Python. With the input left on the master key's path an open chip
  gives `AES(00..00, label)` (measured: the OTP key, the unique key and a zero key in the
  packet encrypt alike; NXP's AN12901 says as much, "If the device is in the Non-secure
  state, SNVS Master key reset to all 0"). The stub and the firmware's module both refuse a
  derived key equal to that value, so a camera never runs on the key every open chip shares.
- **The fuse key.** `fuse_key.c`, on every boot before the key is derived: if the fuse map's
  LOCK word shows SW_GP2 write-locked and read-locked, there is nothing to do. Otherwise the
  stub takes sixteen bytes from the chip's random number generator (refusing a run of one
  value, a repeated word, or a count of ones outside 32..96), burns them into the four
  SW_GP2 words (fuse index 0x29..0x2C), checks each while it can still be read, burns the
  write lock (LOCK bit 21) and then the read lock (bit 23). Fuses only go from 0 to 1, so
  the order is the design: a power cut in the words leaves SW_GP2 unlocked with some bits
  set, and the next boot burns fresh random words on top and carries on; a cut between the
  locks leaves the words final and the next boot burns the read lock. Until both locks are
  in, the stub has no key and boots nothing. The programming sequence and its timing are
  NXP's (`fsl_ocotp.c`), worked out for the clock the stub runs on; no other fuse is written.
- **What the fuses did on the bench** (the fleet's board, an open chip, 2026-11-16; burnt by
  hand from the REPL with the same sequence, one step at a time):

  | Step | What the chip showed |
  |---|---|
  | SW_GP2 blank, DCP input on the fuses | `AES(00..00, block)`: the zero key |
  | A pattern written to the SW_GP2 shadow registers (no fuse burnt) | the DCP encrypts under the pattern: word 0 first, each word little-endian |
  | Four random words burnt | each reads back exactly from the fuse array and the shadows; the DCP's output equals software AES under them; no programming-supply bit (`CCM_CGPR` bit 4) was needed |
  | Write lock burnt (LOCK 0x40128003 to 0x40328003) | the DCP's output is unchanged; a write to a shadow register is refused |
  | Read lock burnt (LOCK to 0x40B28003) | the shadows and the fuse array read 0xBADABADA; **the DCP's output is unchanged** |
  | After a reset | the same |

  Both locks take hold at once, with no reset. The DCP follows its key input only after a
  reset of the DCP itself. Two ways to deny the key to later code were tried and do not
  exist on an open chip: the DCP's `DISABLE_UNIQUE_KEY` capability bit does not take a
  write, and locking the key input (`IOMUXC_GPR10` bit 20) outlasts a warm reset, so a stub
  that locked it away from the fuses would lock itself out on the next boot.

  With the stub and the firmware on the fuse key, the fleet's board, flashed from the
  factory image over what it held under the old key (bench, 2026-11-16): `delta` PASS in
  535 s, `rollback` PASS in 542 s, and after `delta` the read-back from outside the chip:

  ```
  at rest: stub as built; 0 of 166471 firmware blocks stored as built, 0 build samples found, 8.000 bits/byte, 0 bytes not blank after it
  at rest: slot A: 0 of 100096 blocks stored as built, 0 image samples found, 8.000 bits/byte, 0 bytes not blank before its status sector
  at rest: slot B: 0 of 100096 blocks stored as built, 0 image samples found, 8.000 bits/byte, 0 bytes not blank before its status sector
  ```
- **Writes.** The stub's `bee_stream.c` is the counter mode in software; `port_flash_write`
  encrypts a copy of whatever lands in a ciphered range before it programs the page. The
  installer does the same through the `flash_crypt` module, built from that source.
- **The installer's address.** The keystream runs on flash addresses. The RT1062's app
  partition reaches the firmware as a block device, which has none to give, so the firmware
  build bakes the partition's address into `_ota_config.SLOT_CRYPT_BASE` (0x60800000).
- **The lock.** At the jump the stub sets the lock bits of the two ranges' registers: they
  cannot be moved until a reset. The key registers were never readable: with the engine
  running they read back as four zero words.
- **Control data.** A slot's status sector and trailer are its last 8 KiB: one erase block on
  the N6 (64 KiB), two on the RT1062 (4 KiB). The conversion copies both, the trailer's block
  last, so a copy does not verify until it is whole.

Measured on the fleet's OPENMV_RT1060 (bench, 2026-11-14; timestamps from the debug UART):

| Step | Time |
|---|---|
| Factory slot copied into the other slot through the cipher | 28.5 s |
| The clear slot erased | 26 s |
| Firmware installed, encrypted (first boot) | 62 s |
| `delta` (an update written through `flash_crypt`, trial boot, promoted) | PASS, 492 s |
| `rollback` | PASS, 538 s |

The first boot after a factory flash is about two minutes before USB comes up, so `flash
factory` waits five for a board whose stub encrypts.

Read back from the running camera after a conversion: the slot the engine does not cover holds
exactly the bytes the model predicts for the factory image under that slot's tweak, and the
slot the build wrote holds the image in the clear until it is erased.

The fleet's read-back from outside the chip (`check_at_rest`) is a DFU upload on the N6. This
board has no DFU, so `openmv-ota flash read` does the same through its resident bootloader,
which runs before the stub with the engine off: a blhost `read-memory` of the firmware area
(17 s) and of both slots (44 s). After `delta` on the fleet's board (bench, 2026-11-16):

```
at rest: stub as built; 0 of 166461 firmware blocks stored as built, 0 build samples found, 8.000 bits/byte, 0 bytes not blank after it
at rest: slot A: 0 of 100096 blocks stored as built, 0 image samples found, 8.000 bits/byte, 0 bytes not blank before its status sector
at rest: slot B: 0 of 100096 blocks stored as built, 0 image samples found, 8.000 bits/byte, 0 bytes not blank before its status sector
```

The fleet repeats it after every `delta` on this board.

## The AE3, as built (2026-11-16)

On the Alif Ensemble E3 the firmware lives in the chip's own MRAM and the two ROMFS slots on
an OSPI flash part beside it. Only the part can be read off the board, so only the slots are
ciphered: the booted slot's body is ciphertext at rest, and the firmware area is in the clear,
by design.

- **The engine.** It sits on the OSPI controller's memory-mapped read path. Switched on, it
  deciphers every 16-byte block the map returns, anywhere on the part, under one key. It does
  nothing for a write, and the stub never uses the map at all (it reads the part through the
  controller, one command at a time), so the stub runs the cipher in software, both ways.
- **The cipher, measured.** AES-128, one block at a time, with no address in it: the block's
  bytes are reversed going into AES and coming out. The key the engine uses is the sixteen
  bytes handed to the Secure Enclave's key service, reversed. Two reads of erased flash through
  the engine, under two keys, match this model (`tests/build/test_ecb_slot_c.py` holds the
  stub's C to them). Equal plaintext blocks of one image are equal in flash; what keeps images
  apart is the key.
- **The keys.** Each slot image has its own: one AES block under the camera's flash key,
  `tweak || "omvslt"`, the tweak being the same 80 bits the other boards use (slot index and
  the trailer's signed-region hash). The flash key is sixteen random bytes the firmware makes
  on the camera's first boot and keeps in the chip's MRAM, in the first 256 bytes of the key
  area beside the identity keys: four 64-byte places, each a key word (the key, then its
  complement) and a commit word written last. The first whole place is the key; a write a
  power cut stopped is a place that never counts.
- **The first boot.** A camera with no flash key cannot cipher anything: the stub boots the
  slot as it was flashed, in the clear, and says so. boot.py makes the key and resets. On the
  boot after that the stub copies the slot into the other one through the cipher, erases the
  clear one, and installs the firmware from the copy, as on the other boards. `flash
  bootloader` blanks the key area; `flash factory` does not, so a camera that already has its
  key ciphers a newly flashed slot on its first boot.
- **Who switches the engine on.** The firmware, not the stub: the key goes in through the
  Secure Enclave's service, which the stub does not carry. boot.py works out the tweak of the
  slot the stub's install record names, from that slot's trailer, and hands it to the
  `flash_crypt` module, which derives the key, loads it and switches the engine on, before the
  slot is read.
- **Everything else on the part.** The engine cannot be told where to stop, and it cannot be
  switched off around a read either: a bus master reading the slot in that moment (the NPU,
  on a model mapped in place) would get ciphertext. So what is not stored under the engine's
  key -- the other slot, every slot's status sector and trailer, the pad after a slot's
  image, `/flash` -- is read through the map and ciphered back in software, which gives the
  stored bytes exactly. Python does it through `flash_crypt.raw`; `/flash` and the USB drive
  do it through `ospi_flash_read`, the one function they read with, which the firmware build
  replaces at link time (`-Wl,--wrap=ospi_flash_read`, added through the board overlay).
- **Three things the bench showed.**
  - The reset pin restarts the firmware and leaves the engine on, keyed for the slot it last
    booted. `/flash` is mounted before boot.py runs, so the first build read it through a
    cipher it knew nothing about. The stub now switches the engine off on every boot, the
    read hook does the same if it ever meets it on, and boot.py does before it reads anything.
  - The engine takes a key only while it is off. Switched off, keyed and switched on again it
    uses the new key, with no reset in between.
  - With the engine on, the map keeps answering a block it has just read from what it read
    then, even after the part was programmed there: an install counter written and read back
    came back blank. A read somewhere else on the part makes it fetch again, so the module
    makes one, 1 MiB away, before every read of stored bytes.
- **The installer.** It ciphers the slot it writes through `flash_crypt.encrypt`, and reads
  what it wrote, and whether a block is blank, through `flash_crypt.raw`. A delta's base is
  the running slot as mapped as far as its image reaches, and as stored past that.
- **What this does not protect.** The firmware in MRAM is in the clear, and so is the flash
  key beside it: both are inside the chip, and are as private as the chip's debug port. On an
  open camera that is not private. Device lockdown, which closes the port, is not built.

Measured on an OpenMV AE3 over Wi-Fi, each scenario from a factory flash (2026-11-16):

| Scenario | Result | Time |
|---|---|---|
| `delta` | pass | 1154 s |
| `rollback` | pass | 750 s |
| `watchdog` | pass | 740 s |
| `watchdog_recover` | pass | 748 s |
| `coproc` | pass | 662 s |
| `coproc_skip` | pass | 599 s |
| `full` | pass | 713 s |
| `corrupt` | pass | 734 s |
| `bad_sig` | pass | 635 s |
| `reinstall` | pass | 784 s |

The first boot with a flash key copies the slot through the cipher in 26 s and erases the
clear one in 15 s, once. After `delta`, the part read back as stored:

```
at rest: slot A: 0 of 144896 blocks stored as built, 0 image samples found, 8.000 bits/byte, 0 bytes not blank before its status sector
at rest: slot B: 0 of 144896 blocks stored as built, 0 image samples found, 8.000 bits/byte, 0 bytes not blank before its status sector
```

## The H7 Plus and the Pure Thermal, as built (2026-11-18)

The STM32H743 on the OpenMV Cam H7 Plus and the Pure Thermal keeps its firmware in the chip's
own flash and the two ROMFS slots on a QSPI part beside it, like the AE3. Only the part can be
read off the board, so only the slots are ciphered. What the chip lacks is an engine: nothing
deciphers the QSPI part as it is read, so the booted slot is deciphered once, into RAM.

- **The cipher.** The N6 engine's stream, in software (`mce_stream.c`): AES-128 in counter
  mode over the flash address, under the slot's tweak. The same code runs in the stub and in
  the firmware's `flash_crypt` module. A block's position is in the keystream, so equal blocks
  of an image are not equal in flash.
- **The key.** The camera's own, as on the AE3: sixteen random bytes the firmware makes on the
  first boot and keeps in the first 256 bytes of the key area, which here is the last 4 KiB of
  the bootloader's sector in the chip's flash. The reader is one file shared by both boards
  (`lib/aes/flash_key.c`). Internal flash keeps an ECC per 32-byte word and a word a power cut
  tore reads as a bus fault: the stub and the module both read the key area the way that
  turns that into "no key".
- **The stub.** It reads the QSPI part through the controller and runs the stream both ways
  over the one range a slot is selected on, as the AE3's stub does with its cipher. The first
  boot is the AE3's: no key, so the slot boots as flashed; boot.py makes the key and resets;
  the stub then copies the slot into the other one through the cipher and erases the clear
  one. That copy needed two things the H7 stub did not have: a 4 KiB sector erase on the QSPI
  part, and a slot erase block of its own (`STUB_SLOT_BLOCK`), because the install window in
  the chip's flash erases in 128 KiB sectors and the slots do not.
- **The read.** boot.py asks `flash_crypt.load` for the body of the slot the stub's record
  names: the module copies the slot's ciphered range out of the memory map into one block of
  the firmware's frame-buffer pool and runs the stream over it. That copy is what is hashed
  against the trailer and what `/rom` is mounted on. It is as big as the image (the trailer
  says how far the cipher reaches), not the slot. The pool is started over on every soft
  reset, so the copy is made again on each one. A slot that was flashed and not yet ciphered
  starts with the ROMFS magic and is mounted from the map as it is.
- **The installer.** It writes the other slot through `flash_crypt.xor`, as on the N6. A
  delta's base is the RAM copy as far as it reaches and the stored bytes past that (the pad
  after an image is stored blank).
- **What it costs.** RAM: the image's size out of the frame-buffer pool (27 MB on the H7
  Plus, 55 MB on the Pure Thermal). Time: one pass of software AES over the image on every
  boot.
- **Measured** on the fleet's OPENMV4P over Wi-Fi (bench, 2026-11-18), each scenario from a
  factory flash: `delta` PASS (553 s), `rollback` PASS (538 s), `full` PASS (516 s), `corrupt`
  PASS (532 s), `bad_sig` PASS (484 s), `reinstall` PASS (622 s), `watchdog_recover` PASS
  (648 s). On the first boot with a key the stub converted the factory slot in 17.5 s and
  erased the clear one in 11 s, once. Reading the booted slot into RAM (a 1.1 MB image) takes
  about 0.6 s of each boot. The firmware fits with the module in it. After `delta`, the part
  read back from outside the chip over DFU:

  ```
  at rest: slot A: 0 of 68352 blocks stored as built, 0 image samples found, 8.000 bits/byte, 0 bytes not blank before its status sector
  at rest: slot B: 0 of 68352 blocks stored as built, 0 image samples found, 8.000 bits/byte, 0 bytes not blank before its status sector
  ```

  The Pure Thermal builds the same code and has not been run: it has no camera in the fleet.
- **Not the Arduino boards.** The Portenta H7 and the Giga have a 4 MB pool for a slot of up
  to 2 MiB, and the Nicla Vision has no SDRAM at all (476 KB): the copy does not fit a real
  app. Their bootloader is Arduino's, and by its source its USB DFU mode reads the chip's
  flash back (not tried on a board here), so a key kept in the chip would not be private
  there either. Their slots stay in the clear.

## Open questions to verify against documentation

- i.MX RT1062: what closing the chip (HAB) does to the stub's use of the DCP and the BEE, on
  a board that can be closed. (The key is in SW_GP2 and does not depend on it.)

## Not in scope

- Encrypting `/flash` (the user filesystem). It is not load-bearing and holds no code.
- Protecting against an attacker with the device open and SWD available: that is the
  debug lock's job, and it is binary.
- Server-side per-device encryption of releases (see "A/B slots and the key").

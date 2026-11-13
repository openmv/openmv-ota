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

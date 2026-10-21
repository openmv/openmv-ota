# Firmware updates over the air — design

## Context

OpenMV cameras can update their ROMFS app over the air today, but not the C
firmware. The Arduino boards (Portenta H7, Giga, Nicla Vision) have bootloaders we
cannot change, so firmware updates must work from where the firmware normally
starts. The plan: a small C **stub** at the firmware address installs the firmware
carried inside the active ROMFS slot, so one signed ROMFS release becomes the
complete thing a camera runs (platform + app), riding the A/B, delta, trial and
confirm machinery that exists. The same mechanism will later host full-drive
encryption and other pre-boot work.

Trailer v4 is a clean break (nothing is shipped yet): every OTA board gets a stub,
and no board stays on v3.

Status: **design approved 2026-10-20**. *Open* items need a decision before the
code that depends on them.

## Settled decisions

1. **The stub is flashed over USB only and never updates itself.** Verify,
   decompress, copy, jump. Its interface (trailer, status-sector marks, install
   record) is permanent; the fleet will carry mixed stub versions forever.
2. **Firmware inside the ROMFS slot, LZMA-compressed by the tool** (raw LZMA1
   stream, 256 KB dictionary; Python `lzma.FORMAT_RAW` + `FILTER_LZMA1`), once
   per firmware build, bit-identical across releases shipping the same firmware.
   Not compressed on the device (the image is signed as compressed). LZMA over
   deflate to give the ROMFS slots back as much space as possible: measured N6
   1951 → 1066 KB (55 %) vs deflate 1263 KB (64 %); Nicla 1608 → 902 KB vs
   1051 KB; ~150–190 KB per blob, twice per board. The stub has a flash sector to
   itself, so the decoder's extra ~6 KB of code is free; decoder = `LzmaDec.c`
   from the LZMA SDK (public domain), dictionary in RAM during the install only.
3. **The compressed bytes are the source of truth.** The stub verifies the slot's
   signature and the blob's hash, then decompresses those exact bytes to the
   destination; nothing else runs in between.
4. **Space comes out of the ROMFS partition**, not `/flash` (bootloader partition
   tables are fixed). Each A/B slot shrinks by one compressed firmware; the
   firmware area shrinks by the stub's sector.
5. **Portenta and Giga need the LAB LUT off** (192 KB) to fit beside the stub. The
   build forces that imlib flag off. The tool refuses to sign a release whose
   firmware does not fit its destination window.
6. **Delta compression keeps working.** A blob is immutable bytes whose position
   may move every release; the delta's seek makes a moved-but-unchanged blob one
   copy. Changed firmware = full blob download (~1 MB); unchanged = ~1 KB
   (measured 1.2 KB vs 1.22 MB on two AE3 builds).
7. **Trailer = a component list.** Each component is *destination address,
   source offset, size, decompressed size, format, hash*. The stub is a loader:
   it decompresses bytes to an address and checks only that the destination lies
   inside the install window its board config allows. It does not know what the
   bytes are (main firmware, second core, Wi-Fi blob…). `boot.py` hashes only the
   filesystem (`body_sha256`); the stub hashes only the blobs it installs.
8. **Decisions stay in `boot.py`.** The stub reads two existing marks in a slot's
   status sector — *pending* (trial requested) and *confirmed* — plus the attempt
   marks. Its whole rule: boot the pending slot with
   attempts left (burning one attempt), else the confirmed slot; install first if
   the destinations do not already hold that slot's components; if nothing
   verifies, boot what is installed.
9. **State lives in the slots' existing status sectors**, write-once marks only
   (programmed without erase; a torn write reads as "not marked"). One exception:
   the stub's *install record* lives in the firmware area it describes, because it
   must be erased together with that firmware. The install record is also how
   `boot.py` learns which slot the stub chose: **no RAM handoff**.
10. **Trust = one offline root key + certificates.** Stub and firmware embed only
    the root public key (plus a backup root). Every signing key arrives as a
    certificate in the trailer, signed by the root. Revocation = an epoch number
    the stub refuses to go below. Replaces the 32+8 pre-provisioned key pool.
    *Interim (trailer v4 landed, pool not yet removed):* the update **manifest** still
    names its signing key by id, so the firmware keeps a `TRUSTED_KEYS` table for the
    manifest path beside `ROOT_KEYS`; the installer moves to the certificate in the
    `boot.py`/installer step, and the pool shrinks then.
11. **Watchdog and debug UART chosen by the project, used by the stub.** The
    signed header carries the UART (id, baud) and the watchdog (id, timeout) to
    use. The build fills them from the project's device modules — `openmv_log`
    (`ENABLED`, `UART`, `BAUD`) and `openmv_wdt` (new `STUB_WDT_ID`,
    `STUB_TIMEOUT_MS`; its existing WWDG settings are a 100 ms feed cadence that
    cannot span a boot, so the stub's watchdog is a second one, and
    `openmv_wdt.feed()` services both) — so those modules stay the one place a
    project configures them; ids are the firmware's `machine.UART`/`machine.WDT`
    numbering, mapped by a small per-board table in the stub (unknown ids are
    ignored). The other device modules (`openmv_rtc`, `openmv_netcfg`,
    `openmv_cloud`, `openmv_recovery`, `openmv_se`) run inside the firmware and
    need nothing from the stub. A non-zero timeout makes the stub arm that watchdog before jumping,
    so the whole boot and the app are covered, on trial boots and normal ones
    alike. The project's `openmv_wdt` today uses the WWDG (deep-sleep safe,
    but a 167 ms window on the N6 and cleared by reset), which cannot span a boot;
    an IWDG keeps running once started, and whether it freezes in stop/standby is
    a per-chip option byte — a deep-sleeping product leaves it off. `machine.WDT()`
    on the stm32 port reprograms a running IWDG, so the Python API keeps working:
    constructing a WDT over the stub's just changes the timeout. The attempt
    counter still catches crash loops when the watchdog is off.
12. **The IDE is unaware of all this.** Stock firmware over the stub removes OTA
    cleanly; writing ROMFS from the IDE wipes the slots.
13. **Security bar:** anything in writable flash is hostile (slot state included).
    State chooses *among* images that verify, never skips verification. Offsets,
    sizes and destinations come only from the signed trailer and are
    bounds-checked. Only verified bytes reach the decompressor. JTAG/flash-
    programmer access is out of scope.
14. **Debug output.** `boot.py` logs through `openmv_log` (standard `logging`,
    off by default; a build-time flag points it at a `machine.UART`). The stub
    gets a tiny polled UART driver on the UART the trailer names (none = silent). Both the stub and `boot.py` show state on the status LED every
    camera has: one pattern per phase (verifying, installing, trial boot, stopped
    with nothing to boot).
15. **Order:** N6 first (our bootloader, reliable DFU, spare flash; firmware is in
    the same external flash as the ROMFS, XIP at 0x70000000), then AE3 and RT1062,
    then the Arduino and OpenMV H7 boards. Per-chip-family install backends;
    shared formats.

## Slot layout

```
slot start                                                           slot end
| filesystem image | blob 0 | blob 1 | … | 0xFF pad | status (4K) | trailer (4K) |
  ^ body_size
```

- The status sector and trailer sit at the slot end, located from the geometry
  the firmware already knows (`_status_offset` in `boot.py` never used
  `pad_size`). `pad_size` goes; each component carries its own offset and size.
- `body_sha256` covers the filesystem image only (what `boot.py` mounts).
- Blob offsets are absolute in the slot and may move every release.

## Trailer v4

A new fixed header (96 bytes, little-endian, 16-byte aligned fields), then the
component table and the certificate, then the JSON meta. Everything before the
signature is signed. Codec: `src/openmv_ota/ota/trailer.py`.

```
[ header (96) ][ components (n × 48) ][ certificate (160) ][ json_meta ][ signature ][ crc32 ]
└─────────────────────── signed region ──────────────────────┘
```

Header, 96 bytes: framing, versions, stub resources, identity, one reserved
word, digest. Every field is naturally aligned; the u64s sit together.

| Offset | Field | Meaning |
|---|---|---|
| 0 | `magic:4s` | `OMVR` |
| 4 | `header_version:u32` | `4` |
| 8 | `body_size:u32` | filesystem image length |
| 12 | `comp_count:u32` | number of component entries |
| 16 | `meta_size:u32` | JSON blob length |
| 20 | `min_stub:u32` | oldest stub version that can install this image; the tool refuses to build below the fleet's oldest stub |
| 24 | `payload_version:u32` | app version, anti-rollback input, as today |
| 28 | `uart_en:u8` | 1 = the stub logs on the debug UART |
| 29 | `uart_id:u8` | debug UART, in the firmware's `machine.UART` numbering |
| 30 | `wdt_en:u8` | 1 = the stub arms the watchdog before jumping |
| 31 | `wdt_id:u8` | watchdog, in the firmware's `machine.WDT` numbering |
| 32 | `baud:u32` | debug UART baud rate |
| 36 | `wdt_timeout_ms:u32` | watchdog timeout |
| 40 | `product_id:u64` | cross-flash guard, as today |
| 48 | `publish_seq:u64` | account publish counter, as today |
| 56 | `reserved:u64` | 0 |
| 64 | `body_sha256:32s` | hash of the filesystem image |

The signature's algorithm and length come from the certificate (`alg`), which the
verifier reads first; the v3 `sig_alg`/`sig_size` fields are gone.

Component entry, 48 bytes. Every blob is raw LZMA1 (fixed lc/lp/pb, 256 KB
dictionary); a different codec would be a `header_version` bump.

| Offset | Field | Meaning |
|---|---|---|
| 0 | `dest:u32` | absolute flash address the decompressed bytes are written to |
| 4 | `offset:u32` | blob start, relative to slot start |
| 8 | `size:u32` | compressed size |
| 12 | `installed_size:u32` | decompressed size; `dest..dest+installed_size` must lie inside the stub's install window |
| 16 | `sha256:32s` | hash of the compressed blob bytes |

Certificate, 160 bytes:

| Offset | Field |
|---|---|
| 0 | `magic:4s` = `OMVC` |
| 4 | `version:u32` = 1 |
| 8 | `kind:u32` — 0 = OTA signing key, 1 = factory key. Replaces today's "factory key ids are < 0x100": only a factory-signed image's first boot opens the 2 s device-key provisioning window, so a fielded OTA image can never be tricked into re-provisioning keys |
| 12 | `root_id:u32` — which embedded root signed this: 0 or 1 (primary / backup) |
| 16 | `epoch:u32` — anti-revocation counter (no RTC, so a counter; it moves only when a key is retired, so u32 is plenty) |
| 20 | `alg:i32` — COSE id of the signing key (ES256) |
| 24 | `pubkey:65s` + 7 pad — uncompressed P-256 point |
| 96 | `sig:64s` — root's ECDSA over bytes 0..95 |

Verification order (stub and `boot.py` alike): certificate signature against the
embedded root → `epoch ≥` stored epoch → image signature against the certificate's
key → component hashes. Confirming a release raises the stored epoch. The root
signs certificates offline; the everyday (KMS) key signs images. Rotate = new
certificate; retire = higher epoch.

*Open:* desk-key certification without a root ceremony each time (an "issuer" key
certified once by the root, or desk keys certified by the KMS key).

## Status sector marks

Today (per slot, 4 KiB, 16-byte stride; `src/openmv_ota/build/device/boot.py`):
`pending` @0, `tried` @16, `confirmed` @32, install counter @64, attempt marks
@80 (64 × 16), rollback-floor entries after. With the stub:

| Offset | Mark | Written by | Meaning to the stub |
|---|---|---|---|
| 0 | `pending` (exists) | the installer | trial requested: boot this slot next, installing its components first |
| 32 | `confirmed` (exists) | `boot.py`/app on confirm | known good |
| attempt region | `attempt` (exists) | the **stub** (boards with a stub) | one per trial boot; `max_attempts` from the firmware build, default 3 |
| floor region | epoch entries (new): the rollback floor's own `u64 key ‖ u64 ~key` entries, in a second region right after the first's 64 | `boot.py` on confirm; the installer carries it; the factory image seeds it | the certificate epoch floor |

`boot.py` on a stub board never burns an attempt itself; it reads the install
record to learn the running slot.

## Install record

Last 4 KiB of the firmware area, written after all components are installed.
32-byte fixed part, then one hash per component, then the CRC:

| Offset | Field | Meaning |
|---|---|---|
| 0 | `magic:4s` | `OMVI` |
| 4 | `record_version:u32` | `1` |
| 8 | `stub_version:u32` | the stub that wrote this record; the firmware reports it in the check-in, and the tool compares `min_stub` against the fleet's oldest |
| 12 | `slot:u32` | which slot's components are installed (0 = A, 1 = B) |
| 16 | `comp_count:u32` | number of hashes that follow |
| 20 | `reason:u32` | why this boot: 0 normal, 1 trial, 2 reverted (the other slot failed its trial), 3 reinstalled (the firmware area was corrupt) |
| 24 | `reserved:u64` | 0 |
| 32 | `comp_sha256[comp_count]:32s each` | the installed components' compressed-blob hashes, in trailer order |
| 32 + 32·n | `crc32:u32` | over everything before it |

- Boot path: if the chosen slot's component hashes equal the record's and the
  destinations hash to what was installed (one SHA-256 over ~2 MB, tens of ms),
  jump. Otherwise install.
- A power cut mid-install leaves no record → install again (idempotent).
- `boot.py` reads `slot` to know which slot to mount, and `reason` (0 normal,
  1 trial, 2 reverted — the other slot failed its trial, 3 reinstalled — the
  firmware area was corrupt) instead of a RAM handoff. The record is derived from
  verified bytes and needs no signature.
- Versions: the stub identifies components by hash only and never reads a
  firmware version. The running firmware knows its own version as today
  (`omv.version_string()`, `os.uname()`) and reports it in the check-in; the
  release's firmware version and commit are in the trailer's JSON meta
  (`"firmware": {"version", "commit"}`) for the host and the server. The stub's
  own version comes from the record's `stub_version`: Python reads the record and
  never the stub binary.

## Stub algorithm

1. Read both slots' trailers; parse; verify certificate + signature; check product
   id, epoch floor, version floor; check every component's destination is inside
   the install window. Discard any slot that fails.
2. Pick: a `pending` slot with attempts left → burn an attempt, it is the target.
   Else the `confirmed` slot. Else: whatever is installed, if the install record
   verifies; else stop, blink, log over the debug UART (recovery needs USB).
3. If the install record does not match the target's components: verify each
   blob's hash, erase the destinations, decompress into them (sector by sector,
   skipping sectors that already match), write the install record.
4. If the chosen slot's trailer has `wdt_en` set, arm that watchdog.
5. Reset peripherals/clocks to reset state, set VTOR, jump.

Failure cases: an ECC fault reading a torn flash word (STM32H7) is caught and
treated as "corrupt → reinstall". A bad blob fails verification before anything is
erased.

## Per-board flash maps

### OPENMV_N6 (first target)

External flash, XIP at 0x70000000. Partitions fixed by our bootloader
(`boards/OPENMV_N6/boot_config.h`):

| Partition | Range | Today | With the stub |
|---|---|---|---|
| BOOTLOADER | 0x000000–0x080000 | OpenMV bootloader | unchanged |
| FIRMWARE | 0x080000–0x400000 (3.5 MB) | firmware | stub @0x080000 (64 KB budget) · install window 0x090000–0x3FF000 · install record @0x3FF000 |
| FILESYSTEM | 0x400000–0x800000 | `/flash` | unchanged |
| ROMFS0 | 0x800000–0x2000000 (24 MB) | A/B, 12 MB each | A/B, 12 MB each, ~1.05 MB of each now the firmware blob |

The stub reuses the bootloader's XSPI flash driver (16-byte write unit on the
octal-DTR part; memory-mapped reads only outside programming).

### Others (filled in as each is done)

- **AE3:** two components (HP → MRAM `HP` partition, HE → MRAM `HE`); install
  window covers both partitions. The HE romfs stays nested in the filesystem.
- **RT1062:** FlexSPI remap could point execution at a slot with no copy, or copy
  for one shared model. *Open.*
- **Arduino H7:** app area 0x08040000–0x08200000 (14 × 128 KB sectors); stub =
  one sector; firmware ≤ 1.625 MB → LAB LUT off. ROMFS partition 4 MB at
  0x90B00000 → slots ~1.1 MB each after the blob.
- **OPENMV4/4P/PT:** like Arduino H7, behind our bootloader.

## Open questions

1. Desk-key certification path.
2. RT1062: remap vs copy.
3. Write-protect the stub's sector? (little gain: MicroPython cannot easily write
   internal flash)
4. Monotonic counter in the SE050/ATECC608 for the version/epoch floor — later.
5. (settled: LZMA1 via LzmaDec, 256 KB dictionary)
6. The stub's per-board UART/WDT id table (follow the firmware's `machine` ids).
7. IWDG stop/standby freeze option per chip, for products that deep sleep.

## Work plan

1. Settle the formats here (trailer v4, certificate, marks, install record).
2. Tool: trailer v4 codec + component packing + LZMA blob + fit check +
   certificate issue/verify (`src/openmv_ota/ota/trailer.py`,
   `src/openmv_ota/build/romfs.py`, `src/openmv_ota/project/keys.py`); `boot.py`
   reads v4 and the install record. Host tests at 100 % coverage.
3. N6 stub: flash driver from the bootloader, verify (p256-m + SHA-256), LzmaDec,
   install, debug UART. Bench-tested on the N6 with nRST pulsed mid-install.
4. HIL: trial/revert/reinstall legs on the N6; bad signature; revoked epoch; old
   stub given a newer `min_stub`.
5. Server: nothing new beyond serving the bigger image; check-in reports stub
   version and installed component hashes.
6. AE3, RT1062, then the H7 boards.

## Verification

- Host: `pytest` (100 % coverage gate) + `ruff check .` in openmv-ota; round-trip
  tests of pack/parse for trailer v4 and certificates, and `boot.py`'s slot
  selection from the install record.
- Bench (N6): install from slot B, confirm, revert after a failed trial, reinstall
  after a corrupted firmware area, nRST pulsed at random points mid-install, 50+
  cycles each without a brick.
- HIL: the legs in work-plan step 4 on the N6 node, green before the next board.

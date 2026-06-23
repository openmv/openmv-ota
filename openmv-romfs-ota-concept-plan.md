# mboot-style boot.py + ROMFS self-update — concept plan (v8)

> **Amendments since v7 (reconciled with the implemented host tooling).** The
> host side — `openmv-ota romfs` / `project` / `build romfs` — is built and
> tested; the on-device side (boot.py, the verify shim, slot composition, the
> update server) is not yet. Where this plan and the shipped tools disagree, the
> tools and their docs in [docs/](docs/) win. Key changes from v7, each detailed
> in the section it touches:
>
> - **Signatures: ECDSA over the NIST P-curves, not ed25519.** Named by IANA COSE
>   algorithm id (ES256 / P-256 default; ES384/ES512), verified on-device by the
>   **mbedtls already in firmware** — no custom `ed25519_verify` C module. See
>   *Signing* and [docs/trailer.md](docs/trailer.md).
> - **Trailer is a hybrid format, not one fixed 256-byte struct.** A small fixed
>   80-byte trust-header + a length-delimited JSON metadata blob + the signature +
>   a crc32; the signed region is `header ‖ json`. boot.py parses only the header.
>   See *Trailer* below.
> - **Sizing is per-board, from the flash erase block** (`erase_size` in the board
>   config), not a fixed 4 KiB. Trailer/status are one erase block each (floored to
>   4 KiB); boards whose ROMFS is a single large internal-flash sector (OpenMV2/3/4)
>   are detected as **not OTA-capable**. See *Sizing*.
> - **Tooling is one CLI, `openmv-ota`** (`romfs` / `project` / `build`), with a
>   firmware-pegged *project* (lock + config) rather than the four separate
>   `openmv-ota build-*` tools. See *Build tooling*.
> - **New, generated `/rom/system.json`** carries board identity + provenance for
>   the app (OTA or not); the trailer's JSON is a verbatim copy of it.
> - **`board_id` is auto-assigned** (deterministic, per product+board);
>   anti-rollback floor comes from `app/settings.json` `rollback_floor`.

## Context

Goal: a `boot.py` flashed as a frozen module in firmware that picks a ROMFS image to mount, with MCUboot-grade defences against **OTA-borne** threats: cryptographic signatures, key rotation/revocation, anti-rollback, and a one-shot trial-boot rollback. boot.py is intentionally minimal — **it only does ioctl calls and pure computation** (CRC, signature verify, marker reads). No watchdog, no RTC, no machine state. Liveness, retries, and crash recovery are the app's job; if the new image misbehaves, the user / system can simply power-cycle and the trial-boot's "tried" marker takes care of the rollback on the next boot.

ROMFS images carry no native checksum, signature, version, or commit marker — we inject all of that ourselves into a trailer and a small status sector.

**Threat model.** OTA-borne threats only: signed-or-unsigned firmware artefacts pushed at us over the network, with a possibly-controlled network in front of the device. Local USB / SWD / JTAG access, hardware fault injection, and side channels are explicitly **out of scope**. Anyone with bus access on these boards can do anything — that's the deal.

## Hard constraints

- One ROMFS partition. Cannot add a second physical partition.
- Partition is virtually subdivided at the Python level.
- `WRITE_PREPARE(id=0, length)` always erases `[romfs_base, romfs_base + length)` — no offset arg.
- Writes do **not** auto-erase on STM32 / Alif-OSPI. RT1062 blockdev does. AE3-MRAM needs no erase.
- `/flash` unreliable, `/sdcard` not always present, RAM cannot hold a full image.
- No persistent state outside the partition itself. No RTC backup. No `/flash` markers.
- Pure Python in boot.py and the updater for everything *except* primitives that have a C-backed module in firmware. C-backed primitives we use:
  - `hashlib.sha256` and `binascii.crc32` — already in stock MicroPython, fair game.
  - **ECDSA verify over a thin shim on the firmware's mbedtls** (~v3.6.2), which already compiles in ECDSA + the NIST P-curves (secp256r1/384r1/521r1) + SHA-256/384/512 on every board (it's what TLS uses). Default ES256 / P-256 rides the most-exercised path. The shim reads a raw `R‖S` signature + an uncompressed public point, no DER. This replaces the v7 plan's bespoke `ed25519_verify` C module: ed25519 is *not* enabled in firmware's mbedtls, so using it would have meant vendoring a curve library — ECDSA reuses an already-audited, already-compiled primitive. Verify cost is a few ms. (Hardening: RFC 6979 / FIPS 186 test vectors + Wycheproof's ECDSA corpus — invalid `R`/`S`, `s > n`, point-not-on-curve, malformed encodings.)
- boot.py: ioctl + computation only. No watchdog, no machine state, no port-specific calls beyond `vfs.rom_ioctl`. SHA-256, CRC32, and ECDSA verify all delegate to C-backed firmware modules.

## What `vfs.rom_ioctl` does on each board (verified)

| Board | File | Erase | Write | Random offset? |
|---|---|---|---|---|
| **Nicla Vision** STM32H7 QSPI | [stm32/vfs_rom_ioctl.c:106-145](lib/micropython/ports/stm32/vfs_rom_ioctl.c#L106-L145) | `length` bytes from `romfs_base`, 4 KiB blocks | `mp_spiflash_write`, no auto-erase | No |
| **Portenta N6** STM32N6 XSPI | [stm32/vfs_rom_ioctl.c:147-157](lib/micropython/ports/stm32/vfs_rom_ioctl.c#L147-L157) | same | `spi_bdev_writeblocks_raw`, no auto-erase | No |
| **AE3 OSPI** | [alif/vfs_rom_ioctl.c:101-133](lib/micropython/ports/alif/vfs_rom_ioctl.c#L101-L133) | same | `ospi_flash_write`, no auto-erase | No |
| **AE3 MRAM** | [alif/vfs_rom_ioctl.c:147-159](lib/micropython/ports/alif/vfs_rom_ioctl.c#L147-L159) | no-op | 16-byte slots | Yes |
| **RT1062** | [mimxrt/mimxrt_flash.c:189-203](lib/micropython/ports/mimxrt/mimxrt_flash.c#L189-L203) | `mimxrt.Flash` blockdev: `Flash.ioctl(BLOCK_ERASE, n)` | `Flash.writeblocks` auto-erases | Yes |

## The asymmetry that gives us a golden image for free

```
romfs_base ───────────────────────────────────── romfs_base + size
│       FRONT (mutable)         │         BACK (immutable)       │
│  erased by length=FRONT_SIZE  │   only erased by full wipe     │
0                            FRONT_SIZE                  partition_size
```

`vfs.rom_ioctl(3, 0, FRONT_SIZE)` erases only FRONT. There is **no** way to selectively erase BACK. Once the factory writes a golden image into BACK, the application cannot accidentally damage it during a normal update — only a deliberate full-partition wipe can touch BACK. **FRONT = mutable runtime image. BACK = golden, factory-written, immutable.**

## Slot layout

```
┌──────────────── slot (FRONT_SIZE or BACK_SIZE) ──────────────────┐
│ ROMFS body     │ padding (0xFF) │  STATUS sector  │ TRAILER sector│
│ offset 0       │                │  one erase block│ one erase blk │
│ image_size B   │                │  (4 KiB on NOR) │ (4 KiB on NOR)│
└──────────────────────────────────────────────────────────────────┘
```

The STATUS and TRAILER are **one flash erase block each** so they can be rewritten
without disturbing the body. That block is the partition's erase size, floored to
4 KiB (`erase_size` in the board config; 4 KiB on every OTA-capable board, since
their ROMFS lives in external NOR/OSPI or MRAM). So `image_size ≤ SLOT_SIZE −
2·block`, and a partition whose slot can't fit a body after two control blocks is
**not OTA-capable** (see *Sizing*). The host computes all of this in
[`openmv_ota.ota.geometry`](src/openmv_ota/ota/geometry.py).

### Trailer (immutable, written last during update)

> **This supersedes v7's single 256-byte struct.** The trailer is now a *hybrid*
> format: a small fixed trust-header (only the fields boot.py enforces) + a
> length-delimited JSON metadata blob (rich provenance) + the signature + a crc32.
> [docs/trailer.md](docs/trailer.md) and
> [`openmv_ota.ota.trailer`](src/openmv_ota/ota/trailer.py) are the byte-level
> source of truth; the summary here just records the design.

```
[ header (80) ][ json_meta (meta_size) ][ signature (sig_size) ][ crc32 (4) ]
└──────── signed region: header ‖ meta ────────┘
└──────────────── crc32 region: everything before the crc ────────────────┘
```

The trailer occupies one flash erase block (padded with `0xFF`). The **signed
region is `header ‖ meta`** — the signer signs those bytes and the verifier hashes
the identical *stored* bytes, so there's no JSON-canonicalisation trap. The
signature and crc sit outside it. `meta_size` / `sig_size` live in the *signed*
header, so the framing a verifier trusts comes from authenticated fields.

**Fixed header (80 bytes, little-endian), in order:** `magic` (`OMVR` = ROMFS app,
`OMVF` = firmware reserved — the magic doubles as the payload-kind discriminator),
`header_version`, `body_size`, `pad_size`, `meta_size`, `sig_size`, `board_id`,
`min_platform_version`, `payload_version`, `payload_version_floor`, `key_id`,
`sig_alg` (int32 COSE id), `body_sha256` (32 B). Every header field and all the
JSON are authenticated; only `signature` and `crc32` are outside the signed region.

Field semantics (boot.py enforces, app reads via the JSON / `/rom/system.json`):
- `body_sha256` — SHA-256 of the `body_size` body bytes. The hinge: verifying the
  signature over the header + recomputing this hash transitively authenticates the
  large body from a small signed footer.
- `payload_version` (u32, `(major<<24)|(minor<<16)|(patch<<8)|build`) — the
  **monotonic anti-rollback counter / OTA epoch**. boot.py rejects FRONT if
  `payload_version < BACK.payload_version`; the updater enforces a floor too.
  Sourced from `app_version` in `app/settings.json`.
- `payload_version_floor` (u32) — a forward floor this image asserts: every later
  update must be `>=` it (hard CVE cutoff). The updater folds it into
  `new.payload_version >= max(back.payload_version, front.payload_version,
  front.payload_version_floor)`. `0` = no extra floor. Sourced from
  `rollback_floor` in `app/settings.json`.
- `min_platform_version` (u32, same encoding) — minimum *platform* (for a ROMFS app,
  the OpenMV base firmware, which pins MicroPython) the payload needs. boot.py
  rejects `min_platform_version > the running platform`. Derived from the project's
  pegged firmware. `0` = no constraint.
- `board_id` (u32) — cross-flash guard; the device rejects an image whose `board_id`
  ≠ its own. **Auto-assigned** by the host (deterministic, per product+board), so
  vendors never invent the number; `0` = unset (check skipped). See
  [docs/project.md](docs/project.md).
- `key_id` (u32) — selects which baked-in trusted key verifies this image; absent /
  revoked ⇒ reject without verifying. A selector, not trust (COSE `kid`), but it
  sits inside the signed region so it can't be repointed.
- `sig_alg` (int32) — IANA COSE algorithm id (`-7` ES256, `-35` ES384, `-36` ES512).
  Signed, so the algorithm can't be downgraded. `int32` because COSE ids are negative.
- `header_version` — version of *this fixed layout only*; boot.py hard-rejects an
  unknown version (forward-incompatible by design). Additive provenance goes in the
  JSON instead, so this rarely bumps. (Replaces v7's `schema_version`; `sig_alg`
  still gives independent algorithm agility.)
- `pad_size` — `0xFF` bytes between the body and the status/trailer blocks;
  `body_size + pad_size` = where the status block begins, so the slot is
  self-describing across boards with different erase geometry.

**JSON metadata** (length `meta_size`, deterministic `sort_keys` UTF-8): a **verbatim
copy of the image's `/rom/system.json`** — product / board / `board_id` / app
version / firmware-MicroPython-toolchain provenance. boot.py never parses it (the
trust path stays parser-free); the app reads identity from `/rom/system.json`, and
host tools (the update server, an `inspect`) read the same data from the trailer
without mounting the ROMFS. Build provenance (commit hashes, tool versions, build
time) lives here and can grow without a `header_version` bump.

The trailer is written **once** per update, **after** the body has been streamed and
verified. Becoming-valid is the body-write commit. Erased only by the next full slot
erase.

### Status sector (mutable, three progressive markers)

```c
struct slot_status {             // one erase block. All bytes start at 0xFF after erase.
    uint8_t  pending_marker[16];   // updater writes a fixed pattern after the trailer
    uint8_t  tried_marker[16];     // boot.py writes a fixed pattern on first trial boot
    uint8_t  confirmed_marker[16]; // app writes a fixed pattern after self-test passes
    // rest 0xFF
};
```

Each marker is a 16-byte 1→0 monotonic transition (works on raw flash with no erase). Reading any byte that isn't 0xFF or the canonical pattern means "ambiguous → treat as not-set." That handles bit-rot defensively. (On AE3-MRAM, which has no erase and is byte-writable, the status block is reserved a full floored 4 KiB and the updater writes explicit `0xFF` stripes instead of relying on an erase — see *Per-board notes*.)

No tick array, no `failed` marker. The state `pending+tried+!confirmed` **is** the failure indicator. One trial; if the app doesn't confirm, the next boot rejects the slot and BACK takes over.

The fixed patterns can be e.g. `0xA1*16`, `0xA2*16`, `0xA3*16` — choose three byte values with no obvious aliasing.

## boot.py (pure ioctl + computation, ~40 lines)

> **The code block below is the v7 sketch and parses the old fixed struct** (byte
> offsets, `ed25519_verify`, `schema_version`). The *flow* — slot select → verify →
> body-hash → compatibility → anti-rollback → status state machine, fall back to
> BACK — is unchanged and is captured accurately in *What boot.py decides* after
> the block. The real boot.py will parse the **hybrid** trailer (header + JSON, per
> [docs/trailer.md](docs/trailer.md)), verify with the **ECDSA-over-mbedtls** shim
> over the signed region `header ‖ meta`, and read sizes (`TRAILER_SZ`/`STATUS_SZ`)
> as one erase block from build-time board constants. Read the offsets/`ed25519`
> here as illustrative, not normative.

```python
# boot.py — frozen module in firmware. ioctl + computation only. (v7 sketch.)
import vfs, os, sys, struct, binascii, hashlib

FRONT_SIZE     = ...
PARTITION_SIZE = ...
TRAILER_SZ     = 4096
STATUS_SZ      = 4096

TRUSTED_KEYS    = {
    # Substituted in at firmware build time from keys/trusted_keys.json.
    # Each entry is 36 bytes (4-byte key_id + 32-byte ed25519 pubkey).
    # Sized at 256 entries (~9.2 KB of flash) to cover 30-day rotation for
    # 20+ years (20 * 365 / 30 = 244 OTA rotations + factory + emergency + a few
    # special-purpose + buffer). Verify is O(1) by key_id lookup, so unused
    # capacity costs only flash.
    0x00000001: b"...",   # factory key (signs BACK; rotates ~never)
    0x00000002: b"...",   # emergency revocation key (dual-compromise events only)
    0x00000010: b"...",   # OTA pool key #1 (current)
    0x00000011: b"...",   # OTA pool key #2 (next)
    # 0x00000012 .. 0x0000010F: ~244 additional OTA pool slots (pre-baked, unused)
    # 0x00000110+:            dev/test, white-label partners, etc.
}
SUPPORTED_SIG_ALGS = {1}   # 1 = ed25519

PENDING_MARK   = b"\xA1" * 16
TRIED_MARK     = b"\xA2" * 16
CONFIRMED_MARK = b"\xA3" * 16
TRAILER_MAGIC  = 0x4F4D5246

# Build-time constants surfaced from board.json / firmware build:
MAX_SCHEMA_VERSION    = 1
OPENMV_FIRMWARE_VER   = ...   # e.g. (4 << 24) | (5 << 16) | (0 << 8) | 12 for openmv v4.5.0 build 12

# Trailer field offsets (constant, derived from struct definition).
_OFF_SHA              = 32
_OFF_BUILD_TS         = 64
_OFF_FW_VER           = 72
_OFF_MIN_REQ_VER      = 76
_OFF_COMMIT_HASH      = 80
_OFF_RESERVED_SIGNED  = 112
_OFF_SIG              = 128
_OFF_RESERVED_UNSIG   = 192
_OFF_CRC              = 252
_SIGNED_END           = _OFF_SIG    # signature covers bytes [0:128]
_TRAILER_USED         = 256

# Telemetry hooks the app reads after boot.py finishes (module-level).
last_slot             = None     # 'FRONT' or 'BACK'
last_image_version    = 0
last_image_version_back = 0
last_build_timestamp  = 0
last_build_commit     = b""
last_failure_reason   = None     # if FRONT was rejected, why

def _slot(view, offset, size):
    body_view  = view[offset : offset + size - STATUS_SZ - TRAILER_SZ]
    status     = bytes(view[offset + size - STATUS_SZ - TRAILER_SZ : offset + size - TRAILER_SZ])
    trailer    = bytes(view[offset + size - TRAILER_SZ : offset + size])
    return body_view, status, trailer

def _trailer_ok(trailer):
    if struct.unpack_from("<I", trailer, 0)[0] != TRAILER_MAGIC: return None
    schema = struct.unpack_from("<I", trailer, 4)[0]
    if schema == 0 or schema > MAX_SCHEMA_VERSION: return None   # forward-incompatible
    crc_stored = struct.unpack_from("<I", trailer, _OFF_CRC)[0]
    if (binascii.crc32(trailer[:_OFF_CRC]) & 0xFFFFFFFF) != crc_stored: return None
    # magic, schema, image_size, image_version, board_id, key_id, sig_alg, flags,
    # sha[32], build_timestamp, fw_version, min_req_image_version,
    # commit_hash[32], reserved_signed[12], signature[64]
    return struct.unpack_from("<IIIIIIBxxxI32sQII32s12s64s", trailer, 0)
    # tuple indices:
    # 0:magic 1:schema 2:image_size 3:image_version 4:board_id 5:key_id
    # 6:sig_alg 7:flags 8:sha 9:build_ts 10:fw_ver 11:min_req_ver
    # 12:commit_hash 13:reserved_signed 14:signature

def _signature_ok(fields, signed_bytes):
    if (fields[6] not in SUPPORTED_SIG_ALGS): return False
    pubkey = TRUSTED_KEYS.get(fields[5])         # key_id
    if pubkey is None: return False              # revoked or unknown
    return ed25519_verify(pubkey, fields[14], signed_bytes)  # fields[14] = signature

def _compat_ok(fields):
    # Reject images built for newer openmv firmware than what's running.
    # (Since openmv firmware pins a MicroPython version, this implicitly covers MP compat too.)
    img_fw = fields[10]   # firmware_version
    if img_fw != 0 and img_fw > OPENMV_FIRMWARE_VER: return False
    return True

def _markers(s):
    return (s[0:16] == PENDING_MARK,
            s[16:32] == TRIED_MARK,
            s[32:48] == CONFIRMED_MARK)

def _write(slot_offset, status_offset_in_sector, mark):
    return vfs.rom_ioctl(4, 0, slot_offset + FRONT_SIZE - STATUS_SZ - TRAILER_SZ + status_offset_in_sector, mark)
    # Note: this helper is for FRONT only. BACK status is never written by boot.py.

def _body_sha_ok(body_view, image_size, expected_sha):
    h = hashlib.sha256()
    CHUNK = 4096
    for off in range(0, image_size, CHUNK):
        h.update(body_view[off : min(off + CHUNK, image_size)])
    return h.digest() == expected_sha

def _try_mount(view, slot_offset, slot_size, is_front, back_image_version):
    body, status, trailer = _slot(view, slot_offset, slot_size)
    fields = _trailer_ok(trailer)
    if fields is None: raise OSError("trailer")
    image_size = fields[2]
    if image_size > slot_size - STATUS_SZ - TRAILER_SZ: raise OSError("size")
    if not _signature_ok(fields, bytes(trailer[:_SIGNED_END])): raise OSError("sig")
    if not _body_sha_ok(body, image_size, fields[8]): raise OSError("body-sha")
    if not _compat_ok(fields): raise OSError("compat")              # mp_ver / fw_ver
    image_version = fields[3]
    if is_front and image_version < back_image_version: raise OSError("rollback")
    pending, tried, confirmed = _markers(status)
    if is_front:
        if confirmed and not pending and not tried:
            raise OSError("forged-confirm-no-pending")
        if pending and tried and confirmed:
            pass
        elif pending and not tried and not confirmed:
            _write(slot_offset, 16, TRIED_MARK)
        elif pending and tried and not confirmed:
            raise OSError("trial-failed")
        elif pending and not tried and confirmed:
            raise OSError("forged-confirm-no-tried")
        else:
            raise OSError("status-state")
    else:
        if not (confirmed and not pending and not tried):
            raise OSError("back-not-factory")
    vfs.mount(vfs.VfsRom(body[:image_size]), '/rom')
    return fields

# Drop the auto-mount that mp_init did over the whole partition.
try: vfs.umount('/rom')
except Exception: pass

mem = memoryview(vfs.rom_ioctl(2, 0))

# Read BACK's image_version up front — it's the anti-rollback floor for FRONT.
_back_trailer = bytes(mem[PARTITION_SIZE - TRAILER_SZ : PARTITION_SIZE])
_back_fields = _trailer_ok(_back_trailer)
back_image_version = _back_fields[3] if _back_fields else 0
last_image_version_back = back_image_version

try:
    _f = _try_mount(mem, 0, FRONT_SIZE, True, back_image_version)
    last_slot = 'FRONT'
except Exception as _e:
    last_failure_reason = str(_e)
    _f = _try_mount(mem, FRONT_SIZE, PARTITION_SIZE - FRONT_SIZE, False, back_image_version)
    last_slot = 'BACK'

# Expose mounted-image telemetry for the app to read after boot completes.
last_image_version   = _f[3]
last_build_timestamp = _f[9]
last_build_commit    = _f[12]

os.chdir('/rom')
sys.path.append('/rom')
sys.path.append('/rom/lib')
```

What boot.py decides, in order:
1. Trailer `magic == OMVR` + known `header_version` + CRC + size sanity.
2. `sig_alg` supported (a COSE id in the registry) + `key_id` is in `TRUSTED_KEYS` (revocation check).
3. Signature verify (ECDSA via mbedtls) over the signed region `header ‖ meta`.
4. Body SHA-256 matches `trailer.body_sha256` (C-backed `hashlib.sha256`, fed memoryview chunks; tens of ms per MiB).
5. Compatibility: `image.min_platform_version <= OPENMV_FIRMWARE_VER` (don't mount images built for a newer base than what's running). Implicitly covers MicroPython compatibility since the openmv firmware version pins a specific MP version.
6. For FRONT: `payload_version >= back.payload_version` (anti-rollback against the factory floor; the updater also enforces `payload_version_floor`).
7. For FRONT: status state machine
   - `pending && tried && confirmed` → mount (post-OTA confirmed).
   - `pending && !tried && !confirmed` → write `tried`, mount (one-shot trial).
   - `pending && tried && !confirmed` → trial already happened, no confirm → reject.
   - `confirmed && !pending && !tried` → unexpected on FRONT in this design (factory state lives only on BACK); reject.
   - any other → reject.
8. For BACK: status must be exactly factory state (`confirmed` only, `pending` and `tried` both 0xFF). Otherwise reject.
9. If FRONT failed, repeat 1–5 + 8 on BACK. On FRONT rejection, boot.py records the failure reason in module-level `last_failure_reason` for the app to read after boot completes — boot.py doesn't write to UART/REPL because those aren't initialised yet in the frozen-module boot path.
10. Expose telemetry hooks for the app: `boot.last_slot`, `boot.last_payload_version`, `boot.last_payload_version_back`, the mounted image's provenance (from its `/rom/system.json`), and `boot.last_failure_reason`. The app reads these for fleet reporting, rollback UX, and CVE response.

If both fail → exception → REPL. Recovery via DFU (out of scope per threat model).

Why no boot.py watchdog: the user has full control to power-cycle. A hung trial image stays on `pending+tried+!confirmed` after the next reset (the `tried` marker was written before the mount that hung), so the next boot rejects FRONT and falls to BACK. **Liveness is the app's job:** main.py's first responsibility is to arm `machine.WDT` with whatever timeout makes sense for the product. If the app doesn't arm a watchdog, a hung image still rolls back on the next manual reset — slower for the user, but no design weakness on our side.

## Application-side updater (in `/rom`)

> Same caveat as boot.py: the sketch below reads the v7 fixed-offset trailer
> (`image_version` at offset 12, etc.). The real updater reads `payload_version` /
> `payload_version_floor` from the **hybrid** trailer and pads the trailer to one
> erase block; the streaming / read-back / commit-order *flow* is unchanged.

```python
# romfs_update.py  (v7 sketch; offsets illustrative)
import vfs, hashlib, struct, binascii, machine

TRAILER_SZ     = 4096    # one erase block, from board config
STATUS_SZ      = 4096    # one erase block, from board config
FRONT_SIZE     = ...
MAX_IMAGE      = FRONT_SIZE - STATUS_SZ - TRAILER_SZ
PENDING_MARK   = b"\xA1" * 16
CONFIRMED_MARK = b"\xA3" * 16
PARTITION_SIZE = ...

def _read_back_image_version():
    mem = vfs.rom_ioctl(2, 0)
    trailer = bytes(mem[PARTITION_SIZE - TRAILER_SZ : PARTITION_SIZE])
    if struct.unpack_from("<I", trailer, 0)[0] != 0x4F4D5246: return 0
    return struct.unpack_from("<I", trailer, 12)[0]

def _read_front_image_version_if_valid():
    mem = vfs.rom_ioctl(2, 0)
    trailer = bytes(mem[FRONT_SIZE - TRAILER_SZ : FRONT_SIZE])
    if struct.unpack_from("<I", trailer, 0)[0] != 0x4F4D5246: return 0
    crc_stored = struct.unpack_from("<I", trailer, 252)[0]
    if (binascii.crc32(trailer[:252]) & 0xFFFFFFFF) != crc_stored: return 0
    return struct.unpack_from("<I", trailer, 12)[0]

def update(stream, expected_size, expected_sha256, signed_trailer_bytes,
           signature, image_version, board_id):
    if expected_size > MAX_IMAGE:
        raise ValueError("image too large")

    # Floor: BACK is the factory minimum; if FRONT has a valid image, also include its version.
    floor = max(_read_back_image_version(), _read_front_image_version_if_valid())
    if image_version < floor:
        raise ValueError("rollback %d < %d" % (image_version, floor))

    # 1. Erase only FRONT.
    write_align = vfs.rom_ioctl(3, 0, FRONT_SIZE)
    if write_align < 0: raise OSError("erase")

    # 2. Stream body, computing SHA on the fly.
    h = hashlib.sha256()
    offset = 0
    for chunk in aligned_chunks(stream, write_align):
        rc = vfs.rom_ioctl(4, 0, offset, chunk)
        if rc != 0: raise OSError("write")
        h.update(chunk)
        offset += len(chunk)
    if offset != expected_size or h.digest() != expected_sha256:
        raise OSError("body sha")

    # 3. Read-back verify.
    mem = vfs.rom_ioctl(2, 0)
    if hashlib.sha256(bytes(mem[:expected_size])).digest() != expected_sha256:
        raise OSError("read-back")

    # 4. VfsRom parse check.
    vfs.VfsRom(memoryview(mem)[:expected_size])

    # 5. Compose and write the trailer (commit point for the body).
    trailer = compose_trailer(expected_size, image_version, board_id,
                              expected_sha256, signed_trailer_bytes, signature)
    rc = vfs.rom_ioctl(4, 0, FRONT_SIZE - TRAILER_SZ, trailer)
    if rc != 0: raise OSError("trailer")

    # 6. Write the pending marker (commit point for the lifecycle).
    rc = vfs.rom_ioctl(4, 0, FRONT_SIZE - STATUS_SZ - TRAILER_SZ, PENDING_MARK)
    if rc != 0: raise OSError("pending")

    machine.reset()


def confirm():
    """Called by main.py once self-test passes. Idempotent."""
    confirmed_off = FRONT_SIZE - STATUS_SZ - TRAILER_SZ + 32
    mem = vfs.rom_ioctl(2, 0)
    if bytes(mem[confirmed_off : confirmed_off + 16]) == CONFIRMED_MARK:
        return
    rc = vfs.rom_ioctl(4, 0, confirmed_off, CONFIRMED_MARK)
    if rc != 0: raise OSError("confirm")
```

The app's responsibility list:
- Arm `machine.WDT` first thing in `main.py`.
- On first boot of a freshly-installed image: run whatever self-test the product considers definitive (sensors initialised, key services up, etc.); on success, call `romfs_update.confirm()` exactly once. The app decides what "success" means — the design only cares that it's called.
- Drive the OTA channel (TLS, server auth, retries) and call `update()` when ready.
- Decide what to do if `update()` raises (retry, surface to user, etc.).

### Signing

> **Crypto changed from v7's ed25519 to ECDSA over the NIST P-curves.** ed25519 is
> not enabled in firmware's mbedtls; ECDSA + secp256r1/384r1/521r1 + SHA-256/384/512
> already are (TLS uses them), so verify reuses an audited, already-compiled
> primitive instead of a bespoke `ed25519_verify` C module. Algorithms are named by
> IANA COSE id. The host signer is built on the `cryptography` library; see
> [`openmv_ota.ota.sign`](src/openmv_ota/ota/sign.py) /
> [`keys`](src/openmv_ota/ota/keys.py) and [docs/trailer.md](docs/trailer.md).

The customer (firmware developer / fleet operator) owns all keys. We ship the
tooling — keygen, provisioning, image-sign — and define the trailer format. We
never see or store private keys.

- **ECDSA, COSE-named.** Default **ES256 / P-256** (`sig_alg = -7`); ES384/ES512
  available. Signatures are stored raw `R‖S` (host converts DER→raw); public keys
  are the uncompressed EC point (`04 || X || Y`) in hex, which the device's mbedtls
  reads directly via `mbedtls_ecp_point_read_binary`. The signer signs the trailer's
  **signed region** (`header ‖ meta`), not a fixed 128-byte prefix.
- **Provision the whole key set once, at `project new --ota`.** A device trusts
  exactly the public keys baked into its firmware and you can't add one without
  re-flashing, so the host generates the full set up front into
  `keys/trusted_keys.json` (committed) + gitignored private PEMs. Two roles:
  **factory** keys (per manufacturing site; sign the golden BACK image; default 8
  reserve) and an **ota** rotation pool (default 32; over-the-air updates rotate
  through these). The v7 "emergency revocation" / "special-purpose" roles were
  dropped after threat-model review — revocation is `key_id` falling out of the
  baked set at the next firmware build.
- **`key_id`** is assigned by the tooling from well-separated ranges (factory
  `0x0001+`, ota `0x0100+`). The trailer carries the `key_id` that signed it; boot.py
  looks up that one key in its baked-in `TRUSTED_KEYS` (absent ⇒ reject) and reads
  `sig_alg` for the curve + hash. The build firmware step substitutes the trusted
  set + a thin ECDSA-over-mbedtls verify shim into the firmware (no openmv fork —
  injected via `USER_C_MODULES`).
- **`trusted_keys.json` schema**: `{"schema":1,"keys":[{key_id, alg (COSE id), role,
  pubkey (hex point)}, …]}`.

Hardening **requirements** for the device verify shim:
- **Known-answer + negative tests**: FIPS 186 / RFC 6979 ECDSA vectors pass; the
  Project Wycheproof ECDSA corpus is rejected — `r`/`s` out of range, `s > n`,
  point-not-on-curve, non-canonical / truncated / oversized encodings, all-zero `r`
  or `s`.
- **Parser safety**: malformed signature / point inputs fail closed with no crashes
  or out-of-bounds reads; the shim does no dynamic allocation on the verify path.
- Reusing mbedtls means the curve arithmetic is already constant-time and audited —
  the shim is just raw-`R‖S` → MPI marshalling + `mbedtls_ecdsa_verify` over the
  SHA digest of the signed region.

**Important lifecycle reality:** `TRUSTED_KEYS` is baked into the frozen `boot.py`, which lives in the firmware binary. **Rotating *in* a new key is OTA-only; removing a key from `TRUSTED_KEYS` requires a firmware update.** This sounds like a hole, but the threat model around it is what matters:

- The attacker needs *both* a compromised signing key *and* a way to deliver a malicious signed image to devices. Devices pull updates from a customer-controlled server over TLS with cert-pinning. So a leaked key alone is not enough — the attacker also needs to compromise the update server or MITM the connection.
- On key leak: rotate to a pre-populated next key on the build/signing pipeline, stop publishing anything signed with the old key. The leaked key becomes functionally dead — `boot.py` still trusts it, but no legitimate delivery channel ever serves anything signed with it again. No firmware update required.
- The case where actual in-field revocation matters is **dual compromise** (key AND update server) — that's a much higher bar, and it's the situation `K_emergency` + a firmware push is the answer to. Document the limitation, but don't over-engineer for it.

**Pre-populate `TRUSTED_KEYS` at firmware build time with generous rotation headroom.** Each entry costs 36 bytes of flash. Recommended sizing: **256 entries (~9.2 KB)** — covers aggressive 30-day rotation for 20+ years. The math: 20 × 365 / 30 = 244 OTA rotations, plus `K_factory`, `K_emergency`, a few special-purpose slots, and small buffer rounds up to 256 (the next power of 2). Typical layout:

- `K_factory` × 1: signs BACK at manufacturing. Never used for OTA. Stored in the most isolated environment available (dedicated manufacturing HSM, air-gapped). Rotates ~never.
- `K_ota` × ~244: the rotation pool. One is "current" (used by the build pipeline today); the rest are pre-baked but unused, ready to be promoted on routine rotation or compromise. Each rotation just promotes the next slot — no firmware update needed because the new key is already in `TRUSTED_KEYS`.
- `K_emergency` × 1: a long-lived recovery key, **never used for routine signing**. Stored in extreme isolation (HSM, air-gapped, rarely powered on). Used only to sign firmware updates that remove keys from `TRUSTED_KEYS` — i.e., the dual-compromise scenario, where you need to retire a leaked key *and* the attacker has also compromised your update server. Compromising the routine signing pipeline doesn't expose this key, so it remains trustworthy for the recovery firmware push.
- `K_special` × ~10: dev/test, white-label partners, etc., as needed.

**Rotation cadence**: tie OTA-key rotation to the same schedule as the app's pinned TLS cert rotation (the app needs pinned certs to authenticate the update server). Both are trust-anchor lifecycle management; running them on the same cycle keeps the processes uniform and avoids drift between layers. 30-day rotation matches the most aggressive industry trajectory (the public-CA trend is shrinking from 398 days today toward 47 days by 2029) and the 256-slot pool covers it for the full 20-year horizon.

**Routine rotation** (planned hygiene — e.g. annual, or on staff turnover, or on suspected `K_ota` leak):

1. Promote the next pre-baked key in the `K_ota` pool — say from `K_ota_v1` to `K_ota_v2`. Nothing on the device changes; both keys were already in `TRUSTED_KEYS` from factory.
2. Switch the build/signing pipeline to sign new OTA images with `K_ota_v2_priv`.
3. Stop publishing anything signed with `K_ota_v1` on the update server.
4. Audit the update server: any image signed with `K_ota_v1` that you didn't put there is a sign of compromise.

The leaked or rotated key is now functionally dead. `boot.py` still trusts it, but no legitimate delivery channel ever serves anything signed with it. **No firmware update needed.** This works as long as the rotation pool isn't exhausted — at the current sizing (4–8 OTA keys), that's a decade-plus of annual rotations.

**Rotation pool exhaustion** (you've burned through all pre-baked `K_ota` slots):

1. Generate a new rotation pool offline.
2. Ship a firmware release (signed with a still-trusted key) that **replaces** `TRUSTED_KEYS` with a fresh set of slots.
3. This requires devices to actually receive the firmware update. If your fleet doesn't do firmware updates, you've reached the end of your rotation runway — plan ahead by sizing the pool generously at factory time.

**True revocation** (dual-compromise scenario: `K_ota` leaked **and** attacker controls your update server or MITMs your devices):

1. Use `K_emergency` to sign a firmware release that removes the compromised `key_id` from `TRUSTED_KEYS`.
2. Push that firmware to every device via your firmware-update channel (DFU, your firmware-OTA path if you have one, in-field service, etc.).
3. Until a device receives that firmware, it will continue to trust the compromised key — and if the attacker still controls the delivery channel, it will continue to install attacker-signed images. **There is no in-field OTA-only revocation in this design.**
4. BACK is unaffected: signed by `K_factory`, a separate key. Devices tricked into installing a compromised FRONT can still fall back to BACK on the next reset (power-cycle to force).
5. Realistically, dual-compromise of this kind warrants a recall / re-flash anyway, so the firmware-update-required revocation path matches the severity.

**Why not in-field OTA-only revocation?** Two designs would allow it — (1) a signed revocation list stored in ROMFS and consulted by boot.py, (2) a monotonically-increasing minimum-key-version floor in the trailer. Both add substantial boot.py complexity, introduce their own anti-rollback problems (the attacker who has compromised the delivery channel could push old revocation state), and require a separate signing path for the revocation artefact. The pre-populated rotation pool covers the single-compromise case without any of this; the dual-compromise case is severe enough that a firmware push is justified. Deferred.

### Per-board notes

- **STM32H7 / N6 / AE3-OSPI**: code paths above work as written (4 KiB NOR erase blocks).
- **AE3-MRAM**: erase no-op. Updater must explicitly write 0xFF stripes to the status sector after the no-op erase if the underlying MRAM contents aren't already in a known state. (Worth verifying experimentally.) Its 16-byte physical sector is floored to a 4 KiB logical block for the trailer/status (see *Sizing*).
- **RT1062**: `rom_ioctl(3,...)` returns `-EINVAL`. Updater detects and falls through to `mimxrt.Flash` blockdev: `Flash.ioctl(BLOCK_ERASE, n)` per front-slot block, then `Flash.writeblocks(n, chunk, off)` for body / trailer / pending. Boot.py reads via `memoryview(rom_ioctl(2, 0))` (Flash blockdev exposes a buffer interface). Optionally add WRITE_PREPARE/WRITE cases to `mimxrt_flash.c::mp_vfs_rom_ioctl` so RT1062 matches the others — small port-side patch.
- **OpenMV2 / 3 / 4** (internal-flash ROMFS, single large sector): **not OTA-capable** — the host refuses `--ota` for them. They build single non-OTA images.

## Initial / factory state

A small offline tool composes both slot images:
```
slot_bytes    = romfs_image
              + b"\xff" * pad_to_status
              + status_bytes
              + trailer_bytes

# BACK status_bytes — factory state, signed by factory key:
status_bytes  = b"\xff"*16          # pending     — NOT set
              + b"\xff"*16          # tried       — NOT set
              + CONFIRMED_MARK      # confirmed   — set
              + b"\xff"*(STATUS_SZ - 48)

# FRONT status_bytes at factory ship time — same as BACK (factory state).
# After the first OTA, FRONT status will instead transition through pending → tried → confirmed.
```

BACK is signed by the factory key, written once at manufacturing, and never touched again. FRONT ships from the factory in factory state too (mountable without a trial). On the first OTA, FRONT moves into the trial state machine.

The factory provisioning tool sets the trailer fields appropriately: `board_id` to
the product id, `min_platform_version` to the pegged firmware, `payload_version` to
the factory app version, `payload_version_floor` to 0 (no extra floor), `key_id` to
a **factory** key, and the JSON meta (the `/rom/system.json` copy) to the factory
build's provenance — firmware / MicroPython / toolchain versions, commit, build
time. All signed.

**Important:** boot.py's FRONT branch rejects the factory state (`confirmed-only, no pending`) by design — that combination is only valid for BACK. So at first boot from an OTA-untouched device, boot.py mounts BACK directly. To make initial-ship FRONT mountable, the factory tool can write FRONT identically to BACK but the **factory writes BOTH `pending` and `tried` and `confirmed`** so it lands in the post-OTA-confirmed state from FRONT's perspective. That asymmetry is fine: BACK = factory shape, FRONT = post-OTA-confirmed shape.

## Edge-case behaviour

| Scenario | Outcome |
|---|---|
| Healthy update | Updater writes body → trailer → pending. Reset. boot.py: signature OK, version >= floor, pending+!tried+!confirmed → writes `tried`, mounts FRONT. App self-tests, calls `confirm()`. Next boot: pending+tried+confirmed → mount FRONT. |
| Power loss during body write | Trailer never written. Trailer magic fails, fall to BACK. App can retry. |
| Power loss between trailer write and pending write | Trailer valid, pending absent → all-0xFF status → reject FRONT, fall to BACK. App can retry. |
| Power loss between pending write and reset | Same as healthy update; the just-written pending marker steers boot.py through the trial branch. |
| Buggy image — boots, doesn't `confirm()`, reboots (or is power-cycled) | First boot consumed `tried`. Second boot sees pending+tried+!confirmed → reject, fall to BACK permanently for this image. |
| Buggy image — hangs forever | User power-cycles. Same as above. (App is responsible for a watchdog if you want auto-rollback without manual power-cycle.) |
| Tampered body | Boot.py recomputes SHA-256 over the body via C-backed `hashlib.sha256` and compares to the signed `trailer.sha256`. Mismatch → reject FRONT, fall to BACK. |
| Tampered trailer field | Signature verify fails (signature covers all signed fields). Fall to BACK. |
| Replay of old signed image | Updater rejects via floor check (`max(back.image_version, front.image_version_if_valid)`). Boot.py also rejects via `image_version >= back.image_version` if it slips past the updater. |
| Image signed by revoked key | `key_id` not in `TRUSTED_KEYS`. Fall to BACK. |
| Image with unsupported `sig_alg` | Reject. Fall to BACK. |
| Forged-confirm: malicious image stamps `confirmed` but skips `tried` | Caught by `pending && !tried && confirmed → reject`. |
| Forged-confirm: malicious image stamps `confirmed` and `tried` on first boot to skip trial | Impossible from a single boot — `tried` is written by boot.py before the mount, so the very first time the image runs it's already in `pending+tried+!confirmed`. The image cannot stamp `confirmed` *atomically with `tried`*; if the image sets `confirmed` and then crashes or fails to confirm legitimately, the next boot still sees pending+tried+confirmed and trusts it. **Note:** this is a real residual risk — a signed-but-malicious image that sets `confirmed` and survives one boot is fully trusted. Mitigations: (a) make `confirm()` require app-internal evidence beyond just running, and (b) revoke the signing key as soon as compromise is suspected. |
| Bit rot in trailer | CRC32 fails, fall to BACK. |
| Bit rot in body | Boot.py's SHA-256 check catches it; falls to BACK. |
| Bit rot in status sector | Ambiguous bytes treated as "not set." A garbled `confirmed` forces a re-trial — but `tried` is already set, so the re-trial path lands in `pending+tried+!confirmed` → reject → fall to BACK. (Slightly more aggressive than v6, but still safe.) |
| Both slots fail | REPL. Recovery via DFU (out of scope). |
| Refresh the golden | Out of OTA scope. Factory tool only. |

## Sizing

`FRONT_SIZE = BACK_SIZE = partition_size / 2`, rounded **down to the flash erase
block** so FRONT can be erased without disturbing BACK. Each slot loses two erase
blocks to its status + trailer sectors. The block is the partition's `erase_size`
floored to 4 KiB (`MIN_OTA_BLOCK`) — 4 KiB on every OTA-capable board (external
NOR/OSPI; AE3-MRAM's 16-byte sector floored to 4 KiB so growing the trailer JSON
can't reshape a deployed layout). See [`openmv_ota.ota.geometry`](src/openmv_ota/ota/geometry.py).

**Not every board is OTA-capable.** Each partition carries its `erase_size`
(bundled in [`data/boards.json`](src/openmv_ota/data/boards.json), read off the
firmware's flash backend). A board whose ROMFS is a single large internal-flash
sector — **OpenMV2 (128 K), OpenMV3 (256 K), OpenMV4 (128 K)** — has `erase_size`
== the whole partition, so a slot rounds to 0: there's no room for two slots plus
control blocks. `openmv-ota project new --ota` detects this from the geometry alone
and **errors with "not OTA-capable"**; those boards still build a single non-OTA
image filling the partition. OTA-capable: OpenMV4P/PT, RT1062, N6, AE3 (both
cores), Portenta/Giga/Nicla.

Per-board values are resolved into the project lock at `project new` time and
substituted into the frozen `boot.py` / updater at firmware-build time — constants
per build, no runtime introspection.

## What lives where

| Concern | Location |
|---|---|
| Pick which slot to mount | boot.py |
| Verify trailer magic, header_version, CRC, sizes | boot.py |
| Verify signature, key_id, sig_alg (ECDSA via mbedtls) | boot.py |
| Verify body SHA-256 against signed `trailer.body_sha256` | boot.py |
| Verify min_platform_version compatibility | boot.py |
| Anti-rollback floor (FRONT vs back.payload_version) | boot.py |
| Tried marker write on first trial boot | boot.py |
| Expose telemetry (last_slot, last_payload_version, provenance, last_failure_reason) | boot.py module-level |
| Anti-rollback floor (`max(back, front_if_valid, front.payload_version_floor)`) | updater |
| Body streaming, SHA compute, trailer compose & write, pending write | updater |
| `confirm()` after self-test | app (in main.py) |
| Watchdog arming (any flavour) | app — first thing in main.py |
| Decision to retry / give up after rollback | app |
| TLS / cert pinning / mutual auth | app |
| Update authorization, rate limiting, audit logging | app |
| Fleet telemetry / CVE response queries | app (reads boot.py telemetry hooks) |
| Vulnerability disclosure / SBOM / support period | vendor process (tooling generates artefacts) |
| SBOM generation, deterministic builds, hash transparency log | build tooling (openmv-ota repo) |
| Factory provisioning of both slots | offline host tool |

## Build tooling and process requirements (the `openmv-ota` repo)

> **Reconciliation with the implemented CLI.** v7 imagined four separate
> `openmv-ota build-*` tools. The shipped design is **one `openmv-ota` CLI** built
> around a firmware-pegged *project* (a committed lock + config; `openmv-ota
> project new/setup/show/status/sync`). Mapping:
>
> | v7 tool | Implemented |
> |---|---|
> | Tool 3 `build-romfs` | **`openmv-ota build romfs`** — compiles the app + packs the image; for an OTA project also signs + attaches the trailer. Plus `openmv-ota romfs` for verbatim pack/unpack/inspect. **Built.** |
> | Tool 1 `build-firmware` | `openmv-ota build firmware` — bake `boot.py` + `TRUSTED_KEYS` + the ECDSA-over-mbedtls verify shim into firmware. **Reserved (not built).** |
> | Tool 4 `serve` | the update server. **Not built.** |
> | Tool 2 app-side SDK | the on-device `openmv_ota` package (trial-confirm, poll, install). **Not built.** |
>
> Keys, identity, versioning, and provenance are handled as we've now implemented
> them: provision-once `keys/` (factory + ota roles), auto-assigned `board_id`,
> `app/settings.json` (`app_version` + `rollback_floor`), generated
> `/rom/system.json`. The factory-image / slot-composition (`ota factory`) and the
> server are the main unbuilt pieces. Read the tool descriptions below as the
> *intent*; [docs/](docs/) describes what exists.

The repo that clones openmv firmware and injects this OTA design ships the following artefacts alongside the runtime code. These exist to satisfy CRA-style audit/disclosure requirements without burdening boot.py with runtime complexity.

**SBOM generation per build**: every firmware build emits a CycloneDX or SPDX Software Bill of Materials listing all components (MicroPython version, openmv version, mbedtls, lwIP, vendor SDKs, etc.) with version pins and licences. The SBOM is published alongside each firmware release. EU CRA Article 13 effectively requires this.

**Deterministic builds**: the build pipeline produces byte-identical images from the same source. Customers (and outsiders) can independently verify "this binary came from that commit." Requires fixed timestamps, stable file ordering, deterministic compression. Standard practice — Bazel, Nix, or careful Makefile hygiene all achieve this.

**Image hash transparency log**: maintain a public append-only log of every released image's `(image_version, sha256, build_commit_hash, build_timestamp, key_id, board_id)`. Modelled on Certificate Transparency. Lets customers verify their device is running a legitimately-published image (cross-reference its `last_build_commit` and `last_image_version` against the log) and lets outsiders audit for "did the manufacturer ever publish anything I don't have a record of." Cheap to host (a Git repo of append-only JSON works fine).

**`security.txt`** ([RFC 9116](https://www.rfc-editor.org/rfc/rfc9116)): template shipped with the factory ROMFS, pointing security researchers at the customer's vulnerability disclosure process. Required for CRA-aligned responsible disclosure.

**Vulnerability disclosure policy template**: documentation template customers fill in with their security contact, disclosure timeline, scope, etc. Goes in their public-facing site and is referenced by `security.txt`.

**CRA conformity assessment checklist**: a customer-facing document mapping the openmv-ota stack onto each CRA Annex I essential security requirement, showing what's provided by this stack and what the customer must add (app-level concerns, support-period commitment, etc.). Customers include it in their technical documentation when self-certifying.

**HSM-aware signing tooling**: the keygen / image-sign scripts support HSM backends (YubiHSM, AWS CloudHSM, PKCS#11 in general) with a software-keyfile fallback for development. Private keys never leave the HSM. Documented as the recommended path for production keys.

**CVE-scan-at-build**: the build pipeline scans the SBOM against known-vulnerability databases (NVD, OSV) and fails the build (or warns prominently) if any component has an exploitable CVE. CRA Annex I 1(2)(a) ("free of known exploitable vulnerabilities at time of placing on market").

**Factory provisioning tool**: command-line tool that takes a ROMFS body + factory signing key + board.json → produces a flashable BACK+FRONT pair, written to the device over DFU. Used at manufacturing time, never in the field.

## openmv-ota repo: tool deliverables and packaging

The `openmv-ota` repo ships four cooperating tools plus shared assets. They're packaged together (one `pip install openmv-ota` installs everything) so the customer doesn't have to wrangle dependencies between them.

### Bootstrap dependencies (the "untangling" question)

The four tools have a one-way dependency chain — no cycles, despite the appearance:

- **Firmware build** bakes `TRUSTED_KEYS` and `FRONT_SIZE` / `PARTITION_SIZE` / `OPENMV_FIRMWARE_VER` into the frozen `boot.py`. It reads `keys/trusted_keys.json` and `boards/<BOARD>/board.json` from the customer's repo.
- **Boot.py** exposes `boot.FRONT_SIZE`, `boot.PARTITION_SIZE`, and the telemetry hooks at runtime.
- **SDK** is plain Python files in the repo (no per-build generation). It calls into `boot.*` at runtime to learn slot geometry, and it carries the signing-time view (image format, signed-bytes layout) so it can compose update images. The SDK doesn't need any keys baked in — verification keys are firmware-only; signing keys are customer-side, off-device.
- **ROMFS builder** copies the SDK files plus the customer's app into a ROMFS image, computes the SHA, builds the trailer (using `board.json` for `board_id`, customer-supplied `image_version`, etc.), and signs with the specified key from the keys directory. It reads the same `board.json` the firmware build read.
- **Update server** stores signed ROMFS images and serves them; it doesn't sign anything itself, doesn't see private keys.

What needs to be **consistent** across tools is small and explicit — two files in the customer's repo:

- `keys/trusted_keys.json` — public keys, `key_id`s, roles. Read by firmware build (to bake into `TRUSTED_KEYS`) and by ROMFS builder (to know which key signed what).
- `boards/<BOARD>/board.json` — partition size, board_id, firmware version code. Read by firmware build (to set constants) and by ROMFS builder (to know slot sizes and stamp `board_id` into trailers).

Pin one canonical location per file, every tool reads from there.

### Tool 1: Firmware builder (`openmv-ota build firmware`)

Takes: openmv repo URL + commit hash + board target + `board.json` + `trusted_keys.json` + customer metadata (product name, vendor, support period, security contact).

Produces: `firmware.bin` + a directory of audit artefacts:
- `sbom.cdx.json` — CycloneDX SBOM listing every component and version
- `firmware.sha256` + `firmware.signing-cert` (if applicable) — for transparency log entry
- `reproducibility.txt` — toolchain versions, env vars, build timestamp (fixed for reproducibility)
- `conformity-assessment.md` — pre-filled checklist
- `security.txt` — pre-filled from customer metadata
- `cve-report.json` — output of build-time CVE scan against the SBOM

What it does internally:
1. Clones openmv at the specified commit into a build directory.
2. Reads `board.json` → derives sizing constants and `OPENMV_FIRMWARE_VER`.
3. Reads `trusted_keys.json` → generates the `TRUSTED_KEYS` map.
4. Renders `templates/boot.py.in` → frozen `boot.py` with all constants and keys substituted.
5. Injects the thin **ECDSA-over-mbedtls verify shim** as a user C module (via `USER_C_MODULES`, no openmv fork).
6. Patches the firmware Makefile/CMake to add the shim and freeze our `boot.py`.
7. Invokes the openmv firmware build for the target board (which builds mboot + MicroPython + frozen modules into `firmware.bin`).
8. Generates audit artefacts in the output directory.
9. Scans the SBOM against NVD/OSV CVE databases; warns or fails on findings per customer policy.

### Tool 2: App-side SDK (bundled inside the tool installation, not separately installed)

The SDK is a set of Python files (~10 files) shipped *inside* the `openmv-ota` Python package as data files. The ROMFS builder (Tool 3) pulls them from the installed location and copies them into the customer's ROMFS at build time. The customer never copies or imports SDK files directly into their repo — they just write their app against the documented API and let the build process bundle the SDK in.

Customer's app — entire OTA integration is ~20 lines:

```python
# main.py — customer's full OTA integration
import machine, openmv_romfs_ota

wdt = machine.WDT(timeout=30_000)            # liveness

# Customer's network bring-up
import my_network
my_network.connect()

# OTA bootstrap — handles trial confirm, periodic check, install, reset
openmv_romfs_ota.run(
    server_url="https://updates.acme.example",
    self_test=my_self_test_function,          # customer-provided callback
    wdt=wdt,
)

# Customer's actual app
while True:
    wdt.feed()
    do_robot_things()
```

`openmv_romfs_ota.run()` internally:
1. Reads `boot.last_slot`, `boot.last_image_version`, etc.
2. If in trial mode (`last_slot == 'FRONT'`, status sector shows untried-but-pending): runs the customer's `self_test()`. On success → `confirm()`. On failure → log and let the next boot roll back.
3. Reports status to the update server (current version, slot, last failure reason, etc.) for fleet telemetry.
4. Periodically polls the server for available updates.
5. On finding one: downloads, verifies, calls `romfs_update.update()` which resets the device.

SDK module breakdown:
- `openmv_romfs_ota/__init__.py` — public `run()` + `current_version()` + `current_slot()` + `confirm()` API
- `openmv_romfs_ota/_update.py` — the `romfs_update.update()` logic from the plan
- `openmv_romfs_ota/_client.py` — HTTPS client with mandatory TLS + cert pinning
- `openmv_romfs_ota/_telemetry.py` — reads `boot.*` hooks, formats fleet reports
- `openmv_romfs_ota/_audit.py` — `audit_log` pattern
- `openmv_romfs_ota/_streams.py` — chunked-stream helpers for the updater
- `openmv_romfs_ota/cert.pem` — pinned server cert (per-deployment; customer overrides during build)
- Internal helpers for RT1062's blockdev fallback and AE3-MRAM's no-erase path

### Tool 3: ROMFS builder (`openmv-ota build romfs`)

Two modes, same underlying logic:

**Factory mode** (`--mode factory`): composes a full ROMFS partition image (FRONT + BACK identical) signed by the factory key. Flashed at manufacturing time alongside `firmware.bin`. Both slots ship with valid factory-state status sectors so boot.py mounts FRONT directly.

**OTA mode** (`--mode ota`): composes a single signed ROMFS slot (body + trailer, no status markers — those are written by the updater on the device). Output is what gets uploaded to the update server.

Inputs: customer's app directory, `board.json`, `trusted_keys.json` + path to private key (or HSM endpoint), image version, optional `min_required_image_version` floor, `firmware_version` requirement.

What it does:
1. Bundles SDK files (from the tool's own installation) into the build tree.
2. Copies customer's app files (main.py, libs, etc.) into the build tree.
3. Runs the openmv `romfs` tool to compose the ROMFS body.
4. Computes SHA-256, builds the trailer with all metadata, signs with the specified key.
5. For factory mode: composes the full FRONT+BACK partition image.
6. For OTA mode: just the single signed slot.
7. Appends `(version, sha256, build_commit_hash, build_timestamp, key_id, board_id)` to `releases/transparency-log.jsonl`.

### Tool 4: Update server (`openmv-ota serve` + the deployable backend)

**Stateless API + object storage** so it scales horizontally and runs on basically any platform (Render, Fly.io, Cloud Run, AWS, self-hosted).

Public API (devices call):
- `POST /api/v1/check` — device sends `(board_id, image_version, slot, last_failure_reason, build_commit_hash)` → server returns `{available: true|false, version: N, url, size, sha256}`. Per-device or shared-secret authentication.
- `GET /releases/<board>/<version>.bin` — serves the signed image (probably via signed object-storage URL, not through the API server).
- `POST /api/v1/telemetry` — device reports boot success/failure/state for fleet visibility.

Admin API (customer's CI calls):
- `POST /api/v1/admin/release` — upload new signed ROMFS, with rollout policy (canary %, board allowlist, scheduled rollout).
- `GET /api/v1/admin/fleet` — fleet status (version distribution, failure rates).
- `GET /api/v1/admin/audit` — audit log of releases, downloads, telemetry.

Storage: object storage for images (S3, R2, GCS, Azure Blob via a generic adapter), small database for metadata (Postgres or SQLite), append-only transparency log in object storage.

Deploy artefacts: `Dockerfile`, `docker-compose.yml`, `render.yaml`, `fly.toml`, `aws-cdk/` — pick a platform.

Rollout features worth including:
- Canary deployment (serve new version to N% of devices first, gate full rollout on telemetry).
- Board allowlist/blocklist per release.
- Mandatory vs optional updates.
- Scheduled rollout (time-of-day, day-of-week).

### Distribution and packaging

**One `pip install openmv-ota`** installs all four tools as CLI commands:

```
$ pip install openmv-ota
$ openmv-ota init                                       # Scaffolds the customer's repo layout
$ openmv-ota keys generate                              # Creates trusted_keys.json + HSM-bound keys
$ openmv-ota build firmware -c config/firmware.yaml     # Tool 1
$ openmv-ota build romfs --mode factory ... --version 1 # Tool 3 (factory image)
$ openmv-ota build romfs --mode ota     ... --version 2 # Tool 3 (OTA release)
$ openmv-ota serve -c config/server.yaml                # Tool 4 (local dev)
$ openmv-ota publish releases/v2.bin --server URL       # Upload OTA release
```

The SDK files (Tool 2) ship inside the Python package as data files. Tool 3 reads them from the installed location at build time and bundles them into the ROMFS. The customer never installs, copies, or version-pins the SDK separately — its version is determined by which `openmv-ota` version they installed. `pip install openmv-ota==2.3.1` pins everything together for reproducibility.

### Customer repo layout (separate from the openmv-ota tools)

Customer keeps their app and configuration in their own repo:

```
my-product/                          # Customer's repo
├── app/                             # MicroPython app code
│   ├── main.py
│   ├── self_test.py
│   └── lib/
├── config/
│   ├── firmware.yaml                # Tool 1 settings (openmv commit, board, etc.)
│   ├── server.yaml                  # Tool 4 settings
│   └── board.json                   # Or symlinked from openmv-ota's boards/
├── keys/
│   ├── trusted_keys.json            # Public keys + key_ids (committed)
│   └── .gitignore                   # Private keys: HSM-bound, never committed
├── releases/                        # Output: signed ROMFS images
│   ├── v1-factory.bin               # Golden image (manufacturing)
│   ├── v2.bin                       # First OTA release
│   ├── v3.bin                       # Second OTA release
│   └── transparency-log.jsonl       # Append-only release log
├── compliance/                      # Customer-filled-in templates
│   ├── security.txt
│   ├── vuln-disclosure-policy.md
│   ├── conformity-assessment.md
│   ├── eu-doc.md                    # EU Declaration of Conformity
│   └── support-period.md
├── ci/                              # Customer's CI config (if any)
└── README.md
```

Customer's CI invokes the tools; tools read from `config/`, `keys/`, `boards/` (or `board.json`); outputs go to `releases/`. The openmv-ota tools never write to the customer's repo outside `releases/` (and the transparency log).

### Suggested openmv-ota repo layout (the tools repo)

> v7 proposal; the **implemented** layout differs — host code is
> `src/openmv_ota/{romfs,ota,project,build}/` with `data/boards.json` and `docs/`.
> The `firmware_build` / `update_server` / on-device `sdk` subtrees below are the
> unbuilt pieces (the `ecdsa_verify` shim replaces the old `ed25519_verify`).

```
openmv-ota/
├── README.md
├── pyproject.toml                     # Single Python package
├── docs/
│   ├── getting-started.md
│   ├── architecture.md
│   ├── threat-model.md
│   ├── compliance/
│   │   ├── eu-cra-guide.md
│   │   ├── red-3.3-guide.md
│   │   └── conformity-assessment-template.md
│   └── tutorials/
│       ├── first-build.md
│       └── first-ota-release.md
│
├── src/openmv_romfs_ota_tools/              # All four tools live here
│   ├── __init__.py
│   ├── cli.py                         # Single CLI entry point (subcommands)
│   ├── firmware_build/                # Tool 1
│   │   ├── build.py
│   │   ├── templates/boot.py.in
│   │   └── ecdsa_verify/              # ECDSA-over-mbedtls verify shim (user C module)
│   │       ├── ecdsa_verify.c
│   │       ├── micropython.mk
│   │       └── tests/
│   ├── romfs_build/                   # Tool 3
│   │   ├── build.py
│   │   ├── compose.py
│   │   └── sign.py
│   ├── update_server/                 # Tool 4
│   │   ├── app.py
│   │   ├── api/
│   │   ├── storage/
│   │   └── deploy/
│   │       ├── render.yaml
│   │       ├── fly.toml
│   │       └── docker-compose.yml
│   └── sdk/                           # Tool 2 — bundled as package data
│       ├── openmv_ota/
│       │   ├── __init__.py
│       │   ├── _update.py
│       │   ├── _client.py
│       │   ├── _telemetry.py
│       │   ├── _audit.py
│       │   └── _streams.py
│       └── examples/
│
├── boards/                            # Reference board configs
│   ├── OPENMV_N6/board.json
│   ├── OPENMV_NICLAV/board.json
│   ├── OPENMV_AE3/board.json
│   └── ...
│
├── compliance-templates/              # Customer-facing fill-in templates
│   ├── security.txt.template
│   ├── vuln-disclosure-policy.md.template
│   ├── conformity-assessment-checklist.md.template
│   └── eu-doc.md.template
│
└── tests/
    ├── ecdsa_kat/                     # FIPS 186 / RFC 6979 known-answer tests
    ├── wycheproof/                    # Negative-test corpus (ECDSA)
    ├── boot_py_adversarial/           # Boot.py state-machine tests
    └── integration/                   # End-to-end build → flash → OTA
```

### Two open questions worth deciding before building

**1. Is the SDK exposed for customers to pin separately?** Default model: SDK version = tool version, customer pins via `pip install openmv-ota==X.Y.Z`. Alternative: SDK gets its own version, customer can mix-and-match (e.g. older SDK + newer tools). The latter is more flexible but harder to test. Recommend single-version-locked unless a real use case for mixing emerges.

**2. How opinionated is the update server about deployment?** Three options: (a) fully opinionated — one Docker image, customer just deploys it; (b) lightly opinionated — scaffolding + adapters for common storage backends (S3, R2, GCS); (c) pure library — customer builds their own backend. Recommend **(b) lightly opinionated** — one well-tested implementation with adapters for S3-compatible storage and a few hosting platforms, plus a "bring-your-own-backend" interface for sophisticated customers.

## EU CRA / RED 3.3 alignment

The Cyber Resilience Act ([Regulation (EU) 2024/2847](https://eur-lex.europa.eu/eli/reg/2024/2847)) is in force; full compliance deadline 11 December 2027. The Radio Equipment Directive Article 3.3(d)(e)(f) ([Delegated Regulation 2022/30](https://eur-lex.europa.eu/eli/reg_del/2022/30)) is mandatory for radio equipment from 1 August 2025. EN 18031-1/2/3 are the harmonised standards.

This stack supports each relevant requirement as follows:

### CRA Annex I — essential cybersecurity requirements

| Requirement | How this stack supports it |
|---|---|
| 1(2)(a) Free of known exploitable vulnerabilities at placing on market | Build pipeline scans SBOM against NVD/OSV, fails on critical CVEs |
| 1(2)(b) Secure by default configuration | Documented in the conformity assessment template; customer applies |
| 1(2)(c) Security updates throughout support period | OTA mechanism in this plan; vendor commits to a support period |
| 1(2)(d) Protection against unauthorised access | ECDSA (P-256) signatures + anti-rollback + golden-image fallback |
| 1(2)(e) Confidentiality of stored data | **Out of scope** — customer must add encryption if applicable. Documented as explicit non-goal. |
| 1(2)(f) Integrity of stored data | Signature + SHA-256 + CRC32 for ROMFS; customer covers /flash and /sdcard |
| 1(2)(g) Data minimisation | Customer (app design); we provide guidance |
| 1(2)(h) Availability of essential functions | Trial-boot + golden image guarantee one bootable image always exists |
| 1(2)(i) Minimise attack surface | Customer (app design); we provide guidance |
| 1(2)(j) Mitigate impact of incidents | Automatic rollback to BACK on bad update |
| 1(2)(k) Security event recording | `audit_log` pattern provided for the app to implement |
| 1(2)(l) Secure deletion of data | Customer (app design) |
| 1(2)(m) Vulnerability handling throughout support period | OTA delivery + SBOM + transparency log + disclosure template |

### CRA Annex I — vulnerability handling requirements

| Requirement | How this stack supports it |
|---|---|
| 2(1) Identify components in product → SBOM | SBOM generated per build |
| 2(2) Address vulnerabilities promptly | OTA delivery; transparency log enables verification |
| 2(3) Effective testing | Test corpus (RFC 8032, Wycheproof, boot.py adversarial set) |
| 2(4) Public disclosure of fixed vulnerabilities | Disclosure policy template + advisory format |
| 2(5) Coordinated disclosure policy | `security.txt` template |
| 2(6) Mechanism to share vulnerability info | Disclosure policy template defines contact and timeline |
| 2(7) Provide updates without delay, free of charge | OTA delivery, no per-device fee |

### RED 3.3 (Article 3.3(d)(e)(f))

| Requirement | Coverage |
|---|---|
| 3.3(d) Network protection | TLS + cert pinning (app); signatures defend against TLS-layer failure |
| 3.3(e) Personal data protection | Customer (app design); SBOM enables CRA-aligned data-handling audits |
| 3.3(f) Fraud prevention | Signature on every image; anti-rollback prevents downgrade attacks |

### EN 18031 test alignment

Where the harmonised standards specify test cases, this stack maps onto them:
- EN 18031-1 (general): update mechanism (Section 6.x), integrity protection, secure storage of cryptographic material
- EN 18031-2 (data confidentiality): out of scope by design — customer adds encryption
- EN 18031-3 (fraud prevention): signature verification + anti-rollback covers the relevant test cases

### What the customer still owns (CRA compliance is per-product, not per-component)

This stack is a *component*. The customer placing the final product on the market is responsible for:
- Defining and committing to the **support period** (CRA Article 13(2): no shorter than expected product lifetime, minimum 5 years for many categories).
- Producing the **conformity assessment** under CRA Article 32.
- The customer's own **vulnerability handling process** (CRA Article 13).
- Customer-specific security requirements not covered by the OTA mechanism (no default passwords, customer's own network code's secure-by-default config, etc.).
- **EU Declaration of Conformity** and CE marking.

The conformity assessment checklist we ship makes these explicit so nothing is missed.

## Concept scope: explicit non-goals

Features and properties **deliberately not provided** by this design. Documented so customers aren't surprised and so future extensions know where to slot in:

- **Image confidentiality.** Images are signed for authenticity and integrity but **not encrypted**. Anyone who can download from the update server (or sniff a failed/MITM'd TLS connection) can read the image contents. Customers with confidentiality requirements (proprietary firmware, embedded API keys, etc.) must layer encryption on top — out of scope here.
- **Delta / diff updates.** Each update fully overwrites FRONT. Bandwidth-constrained deployments (cellular, LoRa) that want to send only the changed bytes need a delta scheme on top. Not supported in v1; possible future extension.
- **Multi-signature per image.** Trailer carries one `signature` field. N-of-M signing for high-security setups isn't supported; possible future extension via `reserved[]` without a schema bump.
- **In-field OTA-only key revocation.** Removing a key from `TRUSTED_KEYS` requires a firmware update. The pre-populated rotation pool covers the routine compromise case (just rotate to the next pre-baked key and stop publishing the old one). True revocation only matters in the dual-compromise scenario (signing key + update server), which is severe enough to justify a firmware push.
- **Resumable / partial-image updates.** A failed download leaves FRONT in an unmountable state and next boot falls to BACK; the app re-downloads from scratch. No "resume from byte N" support; on slow links the app should buffer the full image (e.g. on /sdcard) before invoking `update()` if it wants resilience to network drops.
- **Persistent counters outside the partition.** No RTC backup, no dedicated metadata sector. Anti-rollback floor is `back.image_version` (factory) — adequate against single-compromise replay, weaker than what a true monotonic counter would give. By design (avoids per-port persistence machinery).

## Out of scope (per threat model)

Pure Python in boot.py cannot defend against any of these — and the user has explicitly accepted this:

- **USB-MSC / USB-CDC abuse, JTAG/SWD readout, RDP defeat, DFU reflash without firmware-level signature.** Anyone with bus access can do anything.
- **Hardware fault injection, side channels.** Hardware-level mitigations only.
- **Network transport attacks.** App-layer (TLS, cert pinning, mutual auth).
- **Compromise of the manufacturer's signing infra.** Mitigated externally; this design supports key rotation/revocation but cannot detect compromise on its own.
- **`boot.py` replacement on /flash.** Requires `boot.py` to be a frozen module inside the firmware image, never a file on `/flash`.
- **Liveness during a hung trial image.** App's responsibility (`machine.WDT`); manual power-cycle is always available as the fallback.

## Open questions

Answered (and now implemented host-side):
- **Sizing**: 50/50, FRONT aligned down to the per-board flash erase block (4 KiB on OTA-capable boards); two control blocks per slot. Boards whose ROMFS is one big internal-flash sector are detected as not OTA-capable. `erase_size` is bundled in `data/boards.json` and resolved into the lock.
- **`FRONT_SIZE` surface**: resolved into the project lock at `project new`; substituted into the frozen `boot.py` at firmware-build time. No runtime introspection.
- **Trailer extensibility**: handled by the JSON metadata blob (additive, signed, app-readable) + `header_version` for trust-path changes. No fixed `reserved` block.
- **Factory key vs. OTA key**: separate roles + `key_id` ranges, provisioned once at `project new --ota` into `keys/trusted_keys.json` + private PEMs.
- **`confirm()` policy**: app calls it on first-boot success per its own definition of "successful."
- **Crypto**: ECDSA over the NIST P-curves (COSE-named; ES256/P-256 default), verified by a thin shim over the firmware's existing mbedtls. No bespoke ed25519 module. `hashlib.sha256` / `binascii.crc32` are the C-backed firmware modules already present.
- **Metadata encoding**: JSON (the trust path doesn't parse it; the app has stdlib `json`). Same bytes packed into `/rom/system.json` and copied into the trailer.

Still worth deciding before implementing the on-device side:
1. **Status-sector format on AE3-MRAM** — the no-erase, byte-writable case (explicit `0xFF` stripes); confirm experimentally.
2. **Update-server deployment opinionatedness** — see the two questions above (lean: lightly opinionated, S3-compatible adapters).
3. **`ota factory` slot composition** — exact on-flash factory-image layout (BACK confirmed-only; FRONT post-OTA-confirmed) and the DFU provisioning flow.

## Critical files / references

Implemented host-side (this repo) — the authoritative format/geometry/keys:
- [src/openmv_ota/ota/trailer.py](src/openmv_ota/ota/trailer.py) — trailer codec (byte-layout SSOT); [docs/trailer.md](docs/trailer.md)
- [src/openmv_ota/ota/algorithms.py](src/openmv_ota/ota/algorithms.py) — COSE algorithm registry · [sign.py](src/openmv_ota/ota/sign.py) · [keys.py](src/openmv_ota/ota/keys.py) · [geometry.py](src/openmv_ota/ota/geometry.py)
- [src/openmv_ota/build/romfs.py](src/openmv_ota/build/romfs.py) — `build romfs` (compile + pack + sign) · [src/openmv_ota/project/](src/openmv_ota/project/) — pegged project / lock · [data/boards.json](src/openmv_ota/data/boards.json) — per-board sizes + `erase_size`
- Docs: [project.md](docs/project.md) · [build.md](docs/build.md) · [architecture.md](docs/architecture.md)

Firmware-side (the on-device pieces, not yet built):
- [lib/micropython/ports/stm32/vfs_rom_ioctl.c](lib/micropython/ports/stm32/vfs_rom_ioctl.c)
- [lib/micropython/ports/alif/vfs_rom_ioctl.c](lib/micropython/ports/alif/vfs_rom_ioctl.c)
- [lib/micropython/ports/mimxrt/mimxrt_flash.c](lib/micropython/ports/mimxrt/mimxrt_flash.c)
- [lib/micropython/extmod/vfs.c:577](lib/micropython/extmod/vfs.c#L577) — auto-mount call
- [lib/micropython/py/runtime.c:199](lib/micropython/py/runtime.c#L199) — auto-mount call site
- [scripts/libraries/_boot.py](scripts/libraries/_boot.py) — current frozen `_boot.py`
- [scripts/libraries/alif/he/_boot.py:58-61](scripts/libraries/alif/he/_boot.py#L58-L61) — `os.chdir("/rom")` precedent

Reference implementations to mine for ideas:
- MCUboot's image format and trailer (`bootutil/include/bootutil/image.h`).
- MicroPython's existing `mboot` C-level bootloader (metadata fields and dual-image flow).

## Verification (when implementing)

Functional:
1. Factory-flash both slots (signed by factory key; BACK = confirmed-only; FRONT = pending+tried+confirmed). Reboot. boot.py mounts FRONT directly via the post-OTA-confirmed branch.
2. Force-clear FRONT to all-0xFF (to simulate uninstalled). Reboot. boot.py rejects FRONT, mounts BACK via the factory-state branch.
3. App calls `update(stream, sig, version)`. Reset. Boot.py: pending+!tried+!confirmed → writes `tried`, mounts FRONT. App calls `confirm()`. Next boot: pending+tried+confirmed → mount FRONT.
4. Buggy image that crashes before `confirm()`. Power-cycle. Boot.py: pending+tried+!confirmed → reject FRONT, mount BACK.
5. Truncated update: trailer never written. Boot.py rejects FRONT, mounts BACK.

Cryptographic / OTA-attack:
6. Tampered body byte post-signing: boot.py's `hashlib.sha256(body) == trailer.sha256` check fails, falls to BACK.
7. Tampered trailer field post-signing: signature verify fails. Fall to BACK.
8. Image signed by revoked key (key_id not in TRUSTED_KEYS): fall to BACK.
9. Image with unsupported sig_alg: reject.
10. Replay of old version via updater: floor check rejects.
11. Replay of old version via direct flash injection: boot.py's `image_version >= back.image_version` rejects.
12. Forged-confirm without `tried`: `pending && !tried && confirmed → reject`.

Robustness:
13. Bit-flip the trailer's CRC field via debugger. Falls to BACK.
14. Bit-flip a status marker byte (0xFF → 0x55). Treated as not-set; behaves correctly.
15. Power-cycle mid-write at random points. Device always boots to a valid `/rom`.

Per-port:
16. RT1062: confirm blockdev fallback path works for erase, body write, trailer write, marker writes.
17. AE3-MRAM: confirm `rom_ioctl(3,...)` no-op leaves slot bytes in known state, or that the updater writes 0xFF stripes to compensate.

boot.py adversarial (the boot.py state machine + trailer parser is itself a security boundary; test it like one):
18. Malformed trailers: random byte garbage, all-0x00, all-0xFF, valid magic but bogus CRC, valid CRC but bogus image_size, valid image_size but past slot end, oversized image_size (e.g. UINT32_MAX), zero image_size, image_size exactly at the boundary (`SLOT_SIZE - 8 KiB`).
19. Malformed status: every combination of pending/tried/confirmed/failed (16 combos), including impossible ones like `tried && !pending`, `failed && confirmed`, all markers set simultaneously. Confirm each combination either mounts correctly or rejects cleanly (no exception leaks past the `_try_mount` boundary).
20. Forged-confirm: image legitimately signed, but its status sector pre-stamped with `pending + confirmed` and `tried` unset on first boot. Boot.py must reject via the `pending && !tried && confirmed` guard.
21. Trailer field swaps: signature still valid for original body+SHA, but `image_version`, `board_id`, or `key_id` swapped after signing. Each must fail signature verify.
22. Replayed older signed image: directly flash a previous-version FRONT image whose signature is still valid. Boot.py's `image_version >= back.image_version` check must reject.
23. Unknown `key_id`: trailer specifies a `key_id` not in `TRUSTED_KEYS`. Must reject without attempting verify.
24. Unsupported `sig_alg`: trailer specifies a value not in `SUPPORTED_SIG_ALGS`. Must reject before invoking the verifier.
25. Future schema_version: trailer has `schema_version` higher than `MAX_SUPPORTED`. Must reject (forward incompatibility).
26. Image targets newer firmware: `firmware_version > OPENMV_FIRMWARE_VER`. Must reject.
27. Old image with newer images installed: install image v50 with `min_required_image_version=50`, then try to install image v45 (signed but older). Updater must reject via floor including `current_front.min_required_image_version`.
28. Telemetry hook integrity: after a successful boot, `boot.last_slot` / `boot.last_image_version` / `boot.last_build_timestamp` / `boot.last_build_commit` / `boot.last_failure_reason` are populated correctly and survive into the app's runtime context.

https://gist.github.com/nickovs/cc3c22d15f239a2640c185035c06f8a3?permalink_comment_id=5456443
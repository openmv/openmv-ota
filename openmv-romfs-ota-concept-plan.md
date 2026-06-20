# mboot-style boot.py + ROMFS self-update — concept plan (v7)

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
  - **`ed25519_verify` — a new C module added to the openmv firmware**, derived from [pmvr/micropython-curve25519](https://github.com/pmvr/micropython-curve25519). That repo wraps Curve25519/X25519 (Montgomery form, key exchange) — we reuse its field-arithmetic kernel and add Edwards-curve point operations + the EdDSA verify routine on top. ~1–2 weeks of work plus hardening (RFC 8032 known-answer vectors + Project Wycheproof's ed25519 corpus: low-order points, malleability, non-canonical encodings, malleable S; parser fuzzing for malformed inputs). Independent of mbedtls compilation flags — works regardless of which TLS primitives a given port has compiled in. Verify cost <100 ms.
- boot.py: ioctl + computation only. No watchdog, no machine state, no port-specific calls beyond `vfs.rom_ioctl`. SHA-256, CRC32, and ed25519 verify all delegate to C-backed firmware modules.

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
│ offset 0       │                │  size − 8 KiB   │ size − 4 KiB  │
│ image_size B   │                │  4 KiB          │ 4 KiB         │
└──────────────────────────────────────────────────────────────────┘
```

`image_size ≤ SLOT_SIZE − 8 KiB`.

### Trailer (immutable, written last during update)

```c
struct slot_trailer {                     // 4 KiB sector; 256 bytes used, rest 0xFF
    // --- Signed region: bytes [0:128] -----------------------------------------
    uint32_t magic;                       // 0x4F4D5246 = "OMRF"
    uint32_t schema_version;              // start at 1
    uint32_t image_size;                  // bytes of body
    uint32_t image_version;               // monotonic build counter / OTA epoch
    uint32_t board_id;                    // sanity check; reject mismatched images
    uint32_t key_id;                      // identifies which signing key was used
    uint8_t  sig_alg;                     // 1 = ed25519. Reserved for future algs.
    uint8_t  pad[3];
    uint32_t flags;                       // reserved bitfield
    uint8_t  sha256[32];                  // SHA-256(body[0..image_size])
    // Provenance / compatibility / fleet metadata (signed):
    uint64_t build_timestamp;             // Unix seconds at sign time; 0 if unused
    uint32_t firmware_version;            // minimum openmv firmware required: see encoding below
    uint32_t min_required_image_version;  // future-floor: next update must be >= this
    uint8_t  build_commit_hash[32];       // full git commit hash (SHA-1 = 20 bytes + 12 zero pad; SHA-256 = 32 bytes)
    uint8_t  reserved_signed[12];         // future signed fields without schema bump
    // --- End signed region (signature covers bytes [0:128]) -------------------
    uint8_t  signature[64];               // ed25519 signature over bytes [0:128]
    uint8_t  reserved_unsigned[60];       // future unsigned fields (rare; mostly unused)
    uint32_t crc32;                       // over bytes [0:252]; protects torn write
};
```

**Signature scope** (pinned for `sig_alg = 1` ed25519): the signed bytes are the entire 128-byte prefix of the trailer (`magic` through `reserved_signed`). Everything that matters for trust or compatibility — body integrity (`sha256`), identity (`board_id`, `key_id`), version (`image_version`, `min_required_image_version`), algorithm (`sig_alg`), build provenance (`build_timestamp`, `firmware_version`, `mp_version`, `build_commit_hash`) — is covered. Any tamper of any signed field breaks the signature.

**Signed metadata semantics** (boot.py enforces, app reads):
- `build_timestamp` (u64, Unix seconds): when the image was signed. Boot.py doesn't check (no RTC), but the app exposes it via telemetry / fleet management for audit ("which build is each device running, when was it released"). The app can refuse-to-run if absurdly old, per its own policy.
- `firmware_version` (u32, encoded as `(major << 24) | (minor << 16) | (patch << 8) | build`): the **minimum** openmv firmware version this image requires. Each component 0–255; gives 256 major releases of headroom (currently 4.x.y; this is comfortable). The `build` byte distinguishes CI builds within the same release. Boot.py compares to its own build-time constant `OPENMV_FIRMWARE_VER`; rejects if `image.firmware_version > OPENMV_FIRMWARE_VER` — i.e. the image requires firmware newer than what's running. Since openmv firmware pins a specific MicroPython version, this implicitly checks MP compatibility too — no separate `mp_version` field needed.
- `build_commit_hash` (32 bytes): the full git commit SHA that produced this image. Pads SHA-1 (20 bytes) with 12 zero bytes; accommodates SHA-256 (32 bytes) directly for the Git-SHA-256 future. Not enforced by boot.py; exposed to the app and the transparency log for SBOM cross-reference, CVE response ("is this device running a build affected by CVE-XXXX?"), and fleet inventory.
- `min_required_image_version` (u32): a "future floor" the image asserts. The **updater** uses this when deciding whether to accept a new image: `new.image_version >= max(back.image_version, current_front.min_required_image_version, current_front.image_version)`. Lets a specific image declare "any future update must be at least version N" — useful for hard CVE cutoffs. Defaults to 0 (no extra floor).

Fields left at 0 are treated as "unset" — defaults to permissive (no constraint).

The trailer is written **once** per update, **after** the body has been streamed and verified. Becoming-valid is the body-write commit. Erased only by the next full slot erase.

**Schema evolution policy.** `schema_version` lets us bump the trailer schema in future firmware releases. Rules:
- Boot.py rejects trailers with `schema_version > MAX_SUPPORTED` — forward-incompatible by design (safer than partially parsing an unknown schema).
- Boot.py may support multiple older schema versions if it needs to verify factory-time BACK signed against an older schema while accepting a newer FRONT.
- The `reserved[124]` block lets us add fields *without* a schema bump if the addition is purely additive (new field, old parsers ignore it). Use a schema bump only when changing semantics or sizes of existing fields.
- `sig_alg` gives independent algorithm agility — schema can stay at v1 while crypto rotates from ed25519 to a future PQ algorithm.

### Status sector (mutable, three progressive markers)

```c
struct slot_status {             // 4 KiB sector. All bytes start at 0xFF after erase.
    uint8_t  pending_marker[16];   // updater writes a fixed pattern after the trailer
    uint8_t  tried_marker[16];     // boot.py writes a fixed pattern on first trial boot
    uint8_t  confirmed_marker[16]; // app writes a fixed pattern after self-test passes
    // rest 0xFF
};
```

Each marker is a 16-byte 1→0 monotonic transition (works on raw flash with no erase). Reading any byte that isn't 0xFF or the canonical pattern means "ambiguous → treat as not-set." That handles bit-rot defensively.

No tick array, no `failed` marker. The state `pending+tried+!confirmed` **is** the failure indicator. One trial; if the app doesn't confirm, the next boot rejects the slot and BACK takes over.

The fixed patterns can be e.g. `0xA1*16`, `0xA2*16`, `0xA3*16` — choose three byte values with no obvious aliasing.

## boot.py (pure ioctl + computation, ~40 lines)

```python
# boot.py — frozen module in firmware. ioctl + computation only.
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
1. Trailer magic + `schema_version <= MAX_SCHEMA_VERSION` + CRC + size sanity.
2. `sig_alg` supported + `key_id` is in `TRUSTED_KEYS` (revocation check).
3. Signature verify over the 128-byte signed prefix.
4. Body SHA-256 matches `trailer.sha256` (C-backed `hashlib.sha256`, fed memoryview chunks; tens of ms per MiB).
5. Compatibility: `image.firmware_version <= OPENMV_FIRMWARE_VER` (don't mount images built for newer firmware than what's running). Implicitly covers MicroPython compatibility since openmv firmware version pins a specific MP version.
6. For FRONT: `image_version >= back.image_version` (anti-rollback against the factory floor).
7. For FRONT: status state machine
   - `pending && tried && confirmed` → mount (post-OTA confirmed).
   - `pending && !tried && !confirmed` → write `tried`, mount (one-shot trial).
   - `pending && tried && !confirmed` → trial already happened, no confirm → reject.
   - `confirmed && !pending && !tried` → unexpected on FRONT in this design (factory state lives only on BACK); reject.
   - any other → reject.
8. For BACK: status must be exactly factory state (`confirmed` only, `pending` and `tried` both 0xFF). Otherwise reject.
9. If FRONT failed, repeat 1–5 + 8 on BACK. On FRONT rejection, boot.py records the failure reason in module-level `last_failure_reason` for the app to read after boot completes — boot.py doesn't write to UART/REPL because those aren't initialised yet in the frozen-module boot path.
10. Expose telemetry hooks for the app: `boot.last_slot`, `boot.last_image_version`, `boot.last_image_version_back`, `boot.last_build_timestamp`, `boot.last_build_commit`, `boot.last_failure_reason`. The app reads these for fleet reporting, rollback UX, and CVE response.

If both fail → exception → REPL. Recovery via DFU (out of scope per threat model).

Why no boot.py watchdog: the user has full control to power-cycle. A hung trial image stays on `pending+tried+!confirmed` after the next reset (the `tried` marker was written before the mount that hung), so the next boot rejects FRONT and falls to BACK. **Liveness is the app's job:** main.py's first responsibility is to arm `machine.WDT` with whatever timeout makes sense for the product. If the app doesn't arm a watchdog, a hung image still rolls back on the next manual reset — slower for the user, but no design weakness on our side.

## Application-side updater (in `/rom`)

```python
# romfs_update.py
import vfs, hashlib, struct, binascii, machine

TRAILER_SZ     = 4096
STATUS_SZ      = 4096
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

The customer (firmware developer / fleet operator) owns all keys. We ship the tooling — keygen, image-sign, factory-provisioning — and define the trailer format. We never see or store private keys.

- Generate ed25519 keypairs offline (the tool we ship runs `openssl genpkey -algorithm ed25519` or equivalent). Private key stays on the customer's HSM / air-gapped signing machine; public key + `key_id` is committed to `keys/trusted_keys.json` in the firmware source tree.
- The build system reads `keys/trusted_keys.json` and substitutes the `TRUSTED_KEYS` map into the frozen `boot.py` at firmware-compile time. Soft cap of ~16 entries (verify is O(1) by `key_id`, so unused entries cost only flash — 36 bytes each).
- `key_id` is a 32-bit value chosen by the customer (sequential numbers, dates, fingerprints, whatever scheme makes sense for their key-management process). The trailer carries the `key_id` of whichever key signed the image; boot.py looks up that single key for verification.
- The build process signs the canonical signed bytes (`magic` through `sha256` of the trailer) and embeds the 64-byte signature plus `key_id` into the trailer.
- Verification: new C module in openmv firmware (`ed25519_verify`), derived from [pmvr/micropython-curve25519](https://github.com/pmvr/micropython-curve25519) — reuse its field-arithmetic kernel and add Edwards-curve point operations + EdDSA verify on top. Independent of mbedtls TLS configuration. Verify cost <100 ms.

Hardening **requirements** for the C module (not just "nice to have"):
- **Known-answer tests**: all RFC 8032 ed25519 test vectors pass.
- **Negative tests**: Project Wycheproof's ed25519 corpus — low-order points, malleability, non-canonical encodings, malleable S, all-zero R, all-zero S, signatures with `S >= L`, etc. — all rejected.
- **Parser fuzzing**: malformed inputs (truncated pubkey, oversized signature, garbage bytes) fail safely with no crashes or out-of-bounds reads.
- **Constant-time scalar multiplication**: required. The verification key is public so timing leaks here are less catastrophic than for signing, but constant-time math is the standard, prevents implementation-bug categories that have historically bitten ECDSA libraries, and is cheap to get right if you start with it.
- **No dynamic allocation on the verify path**: pre-allocate working buffers. Avoids surprises around heap fragmentation during boot.

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

- **STM32H7 / N6 / AE3-OSPI**: code paths above work as written.
- **AE3-MRAM**: erase no-op. Updater must explicitly write 0xFF stripes to the status sector after the no-op erase if the underlying MRAM contents aren't already in a known state. (Worth verifying experimentally.)
- **RT1062**: `rom_ioctl(3,...)` returns `-EINVAL`. Updater detects and falls through to `mimxrt.Flash` blockdev: `Flash.ioctl(BLOCK_ERASE, n)` per front-slot block, then `Flash.writeblocks(n, chunk, off)` for body / trailer / pending. Boot.py reads via `memoryview(rom_ioctl(2, 0))` (Flash blockdev exposes a buffer interface). Optionally add WRITE_PREPARE/WRITE cases to `mimxrt_flash.c::mp_vfs_rom_ioctl` so RT1062 matches the others — small port-side patch.

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

The factory provisioning tool sets the trailer fields appropriately: `build_timestamp` to manufacturing time, `mp_version`/`firmware_version` to whatever this build supports, `build_commit_hash` to the git SHA of the factory build, `min_required_image_version` to 0 (no extra floor), and `key_id` to `K_factory`'s id. These are all signed.

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

`FRONT_SIZE = BACK_SIZE = partition_size / 2`, rounded down to a multiple of 4 KiB. Each slot loses 8 KiB to status + trailer.

Per-board values come from the openmv board JSON build settings: each `boards/<BOARD>/board.json` (or equivalent) declares its ROMFS partition size, and the build system substitutes the resulting `PARTITION_SIZE` and `FRONT_SIZE` into the frozen `boot.py` (and exposes them to the updater) at firmware-compile time. No runtime introspection of the partition needed — these are constants per build.

## What lives where

| Concern | Location |
|---|---|
| Pick which slot to mount | boot.py |
| Verify trailer magic, schema, CRC, sizes | boot.py |
| Verify signature, key_id, sig_alg | boot.py |
| Verify body SHA-256 against signed `trailer.sha256` | boot.py |
| Verify firmware_version compatibility | boot.py |
| Anti-rollback floor (FRONT vs back.image_version) | boot.py |
| Tried marker write on first trial boot | boot.py |
| Expose telemetry (last_slot, last_image_version, last_build_timestamp, last_build_commit, last_failure_reason) | boot.py module-level |
| Anti-rollback floor (`max(back, front_if_valid, front.min_required_image_version)`) | updater |
| Body streaming, SHA compute, trailer compose & write, pending write | updater |
| `confirm()` after self-test | app (in main.py) |
| Watchdog arming (any flavour) | app — first thing in main.py |
| Decision to retry / give up after rollback | app |
| TLS / cert pinning / mutual auth | app |
| Update authorization, rate limiting, audit logging | app |
| Fleet telemetry / CVE response queries | app (reads boot.py telemetry hooks) |
| Vulnerability disclosure / SBOM / support period | vendor process (tooling generates artefacts) |
| SBOM generation, deterministic builds, hash transparency log | build tooling (openmv-romfs-ota repo) |
| Factory provisioning of both slots | offline host tool |

## Build tooling and process requirements (the `openmv-romfs-ota` repo)

The repo that clones openmv firmware and injects this OTA design ships the following artefacts alongside the runtime code. These exist to satisfy CRA-style audit/disclosure requirements without burdening boot.py with runtime complexity.

**SBOM generation per build**: every firmware build emits a CycloneDX or SPDX Software Bill of Materials listing all components (MicroPython version, openmv version, mbedtls, lwIP, vendor SDKs, etc.) with version pins and licences. The SBOM is published alongside each firmware release. EU CRA Article 13 effectively requires this.

**Deterministic builds**: the build pipeline produces byte-identical images from the same source. Customers (and outsiders) can independently verify "this binary came from that commit." Requires fixed timestamps, stable file ordering, deterministic compression. Standard practice — Bazel, Nix, or careful Makefile hygiene all achieve this.

**Image hash transparency log**: maintain a public append-only log of every released image's `(image_version, sha256, build_commit_hash, build_timestamp, key_id, board_id)`. Modelled on Certificate Transparency. Lets customers verify their device is running a legitimately-published image (cross-reference its `last_build_commit` and `last_image_version` against the log) and lets outsiders audit for "did the manufacturer ever publish anything I don't have a record of." Cheap to host (a Git repo of append-only JSON works fine).

**`security.txt`** ([RFC 9116](https://www.rfc-editor.org/rfc/rfc9116)): template shipped with the factory ROMFS, pointing security researchers at the customer's vulnerability disclosure process. Required for CRA-aligned responsible disclosure.

**Vulnerability disclosure policy template**: documentation template customers fill in with their security contact, disclosure timeline, scope, etc. Goes in their public-facing site and is referenced by `security.txt`.

**CRA conformity assessment checklist**: a customer-facing document mapping the openmv-romfs-ota stack onto each CRA Annex I essential security requirement, showing what's provided by this stack and what the customer must add (app-level concerns, support-period commitment, etc.). Customers include it in their technical documentation when self-certifying.

**HSM-aware signing tooling**: the keygen / image-sign scripts support HSM backends (YubiHSM, AWS CloudHSM, PKCS#11 in general) with a software-keyfile fallback for development. Private keys never leave the HSM. Documented as the recommended path for production keys.

**CVE-scan-at-build**: the build pipeline scans the SBOM against known-vulnerability databases (NVD, OSV) and fails the build (or warns prominently) if any component has an exploitable CVE. CRA Annex I 1(2)(a) ("free of known exploitable vulnerabilities at time of placing on market").

**Factory provisioning tool**: command-line tool that takes a ROMFS body + factory signing key + board.json → produces a flashable BACK+FRONT pair, written to the device over DFU. Used at manufacturing time, never in the field.

## openmv-romfs-ota repo: tool deliverables and packaging

The `openmv-romfs-ota` repo ships four cooperating tools plus shared assets. They're packaged together (one `pip install openmv-romfs-ota` installs everything) so the customer doesn't have to wrangle dependencies between them.

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

### Tool 1: Firmware builder (`openmv-romfs-ota build-firmware`)

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
5. Copies the `ed25519_verify` C module into `ports/<port>/modules/` of the openmv tree.
6. Patches the firmware Makefile/CMake to add the ed25519 module and freeze our `boot.py`.
7. Invokes the openmv firmware build for the target board (which builds mboot + MicroPython + frozen modules into `firmware.bin`).
8. Generates audit artefacts in the output directory.
9. Scans the SBOM against NVD/OSV CVE databases; warns or fails on findings per customer policy.

### Tool 2: App-side SDK (bundled inside the tool installation, not separately installed)

The SDK is a set of Python files (~10 files) shipped *inside* the `openmv-romfs-ota` Python package as data files. The ROMFS builder (Tool 3) pulls them from the installed location and copies them into the customer's ROMFS at build time. The customer never copies or imports SDK files directly into their repo — they just write their app against the documented API and let the build process bundle the SDK in.

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

### Tool 3: ROMFS builder (`openmv-romfs-ota build-romfs`)

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

### Tool 4: Update server (`openmv-romfs-ota serve` + the deployable backend)

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

**One `pip install openmv-romfs-ota`** installs all four tools as CLI commands:

```
$ pip install openmv-romfs-ota
$ openmv-romfs-ota init                                       # Scaffolds the customer's repo layout
$ openmv-romfs-ota keys generate                              # Creates trusted_keys.json + HSM-bound keys
$ openmv-romfs-ota build-firmware -c config/firmware.yaml     # Tool 1
$ openmv-romfs-ota build-romfs --mode factory ... --version 1 # Tool 3 (factory image)
$ openmv-romfs-ota build-romfs --mode ota     ... --version 2 # Tool 3 (OTA release)
$ openmv-romfs-ota serve -c config/server.yaml                # Tool 4 (local dev)
$ openmv-romfs-ota publish releases/v2.bin --server URL       # Upload OTA release
```

The SDK files (Tool 2) ship inside the Python package as data files. Tool 3 reads them from the installed location at build time and bundles them into the ROMFS. The customer never installs, copies, or version-pins the SDK separately — its version is determined by which `openmv-romfs-ota` version they installed. `pip install openmv-romfs-ota==2.3.1` pins everything together for reproducibility.

### Customer repo layout (separate from the openmv-romfs-ota tools)

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
│   └── board.json                   # Or symlinked from openmv-romfs-ota's boards/
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

Customer's CI invokes the tools; tools read from `config/`, `keys/`, `boards/` (or `board.json`); outputs go to `releases/`. The openmv-romfs-ota tools never write to the customer's repo outside `releases/` (and the transparency log).

### Suggested openmv-romfs-ota repo layout (the tools repo)

```
openmv-romfs-ota/
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
│   │   └── ed25519_verify/            # C module source
│   │       ├── ed25519_verify.c
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
│       ├── openmv_romfs_ota/
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
    ├── ed25519_kat/                   # RFC 8032 known-answer tests
    ├── wycheproof/                    # Negative-test corpus
    ├── boot_py_adversarial/           # Boot.py state-machine tests
    └── integration/                   # End-to-end build → flash → OTA
```

### Two open questions worth deciding before building

**1. Is the SDK exposed for customers to pin separately?** Default model: SDK version = tool version, customer pins via `pip install openmv-romfs-ota==X.Y.Z`. Alternative: SDK gets its own version, customer can mix-and-match (e.g. older SDK + newer tools). The latter is more flexible but harder to test. Recommend single-version-locked unless a real use case for mixing emerges.

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
| 1(2)(d) Protection against unauthorised access | ed25519 signatures + anti-rollback + golden-image fallback |
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

Answered:
- **Sizing**: 50/50, partitioned from per-board JSON build settings.
- **`FRONT_SIZE` surface**: build-time substitution from the board JSON into the frozen `boot.py`. No runtime introspection.
- **Trailer `reserved[124]`**: leave it as raw reserved bytes; no extra fields needed. API-mismatch failures surface as runtime crashes, which is acceptable.
- **Factory key vs. OTA key**: separate keys, separate `key_id`s. Customer manages both.
- **`confirm()` policy**: app calls it on first-boot success per its own definition of "successful."
- **Crypto**: new `ed25519_verify` C module added to openmv firmware, derived from pmvr/micropython-curve25519's field-arithmetic kernel + new Edwards-curve and EdDSA verify on top. `hashlib.sha256` and `binascii.crc32` come from the C-backed firmware modules already present.

Still worth deciding before implementing:
1. **JSON schemas** — two files:
    - `boards/<BOARD>/board.json` — per-board sizing. Suggested keys: `romfs_partition_size`, derived `romfs_front_size = romfs_partition_size // 2 & ~0xFFF`.
    - `keys/trusted_keys.json` — fleet-wide trusted key list. Suggested shape: `[{key_id: 0x..., pubkey_hex: "...", role: "factory|ota|dev|partner-foo", notes: "..."}]`. The build system flattens this into the `TRUSTED_KEYS` map in the frozen `boot.py`. Adding or removing keys is a firmware-release operation.
3. **Tooling we ship to the customer** — at minimum: keygen wrapper, image-signer (takes ROMFS body + signing key + version + board_id → writes a flashable slot image), factory-provisioning binary (writes BACK + FRONT slots over DFU). Worth a separate planning pass before building.

## Critical files / references

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
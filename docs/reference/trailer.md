# The signed trailer format

The **trailer** is the signed footer at the end of every OTA image. It carries a
SHA-256 of the image body, a table of the **components** the boot stub installs
(the firmware the slot carries), the **certificate** of the key that signed the
image, the targeting and anti-rollback fields a camera checks before mounting the
body, an ECDSA signature, and a JSON blob of build provenance.

The tutorial covers both ends of its life: [release
artifacts](../tutorial/08-release-artifacts.md) is the host side (how `build`
signs it and `inspect`/`verify` read it back), and [boot and
rollback](../tutorial/11-boot-and-rollback.md) is the camera side (`boot.py`
verifying it at every boot). This page is the byte-level on-flash format those
two sides agree on; the codecs —
[src/openmv_ota/ota/trailer.py](../../src/openmv_ota/ota/trailer.py) and
[src/openmv_ota/ota/certificate.py](../../src/openmv_ota/ota/certificate.py) — are
its source of truth.

## Layout

The trailer occupies one 4 KiB control block on every OTA-capable board —
deliberately sized to 4 KiB rather than the flash erase block. Control data is
never erased independently of its slot, so a large-erase board doesn't inflate
the sector, and a board reporting a tiny sector (byte-writable MRAM) is floored
to the same 4 KiB: growing the metadata can never reshape the layout. Laid out
little-endian:

```
[ header (96) ][ components (n × 48) ][ certificate (160) ][ json_meta (meta_size) ][ signature ][ crc32 (4) ]
└──────────────────────────── signed region ────────────────────────────┘
└──────────────── crc32 region: everything before the crc ─────────────────────────┘
```

- The **signed region** is everything before the signature. The signer signs those
  bytes; the verifier hashes the identical *stored* bytes (never a re-serialisation,
  so there is no JSON-canonicalisation pitfall). Every header field, every
  component, the certificate and all the JSON provenance are therefore
  authenticated.
- The **signature** and **crc32** necessarily sit outside the signed region. The
  CRC is torn-write detection only (a cheap pre-reject), not authenticity.
- `comp_count` and `meta_size` live *inside* the signed header, so the framing a
  verifier trusts comes from authenticated fields. The signature's algorithm and
  length come from the certificate's `alg`, which the verifier reads first.
- `build romfs` pads the trailer with `0xFF` to fill its 4 KiB block.

## Header fields

The fixed header is 96 bytes, in four groups: framing, versions, the resources the
boot stub uses, identity, then the digest. Every field is naturally aligned; the
three 64-bit fields sit together at offsets 40, 48 and 56.

| Offset | Field | Type | Meaning |
|---|---|---|---|
| 0 | `magic` | `4s` | `OMVR` = ROMFS app image. The first cheap reject. |
| 4 | `header_version` | `uint32` | Layout version of *this fixed header* (`4`). `boot.py` and the boot stub hard-reject any other version rather than mis-parse it — and because the stub is flashed over USB only, the layout is a commitment, not a negotiation. (`3` had no components or certificate and named its key by id.) |
| 8 | `body_size` | `uint32` | Length of the ROMFS body before the components; bounds the mount and the body hash. |
| 12 | `comp_count` | `uint32` | Number of component entries that follow the header (at most 16). |
| 16 | `meta_size` | `uint32` | Byte length of the JSON metadata blob. |
| 20 | `min_stub` | `uint32` | The oldest boot-stub version that can install this image; `0` = any. |
| 24 | `payload_version` | `uint32` | The app's `app_version` (from `settings.json`), encoded `(major<<24)\|(minor<<16)\|(patch<<8)\|build` so versions compare as plain integers. It is the **anti-rollback input** for a camera with a real `product_id`: the installer and `boot.py` reject an image below the device's recorded floor, and `confirm()` raises that floor to this value. It never *orders* the slots — the install counter does — which is what keeps reinstalling the same version legal. |
| 28 | `uart_en` | `uint8` | `1` = the boot stub logs on the debug UART below. |
| 29 | `uart_id` | `uint8` | That UART, in the firmware's `machine.UART` numbering. |
| 30 | `wdt_en` | `uint8` | `1` = the boot stub arms the watchdog below before jumping to the firmware. |
| 31 | `wdt_id` | `uint8` | That watchdog, in the firmware's `machine.WDT` numbering. |
| 32 | `baud` | `uint32` | The debug UART's baud rate. |
| 36 | `wdt_timeout_ms` | `uint32` | The watchdog timeout. |
| 40 | `product_id` | `uint64` | Target product id; the cross-flash guard. The build auto-assigns a nonzero id — the low 63 bits of `sha256("<product>:<board>")` — so this is `0` (check skipped) only if the project sets it to `0` on purpose ([Integrating as a platform](../tutorial/25-platform-integration.md)). 32 bits collide at a few thousand products, and two product lines sharing an id means one line's devices accept the other's firmware. |
| 48 | `publish_seq` | `uint64` | The account's **publish counter**: one number per build, taken from the update server, strictly increasing across every product the account publishes. It is the anti-rollback input for a camera built with `product_id = 0` — the only camera that can be moved between products, whose images a per-product version cannot order. `0` for every other project, and ignored by every other camera. |
| 56 | `reserved` | `uint64` | Zero. The headroom the next field comes out of. |
| 64 | `body_sha256` | `32s` | SHA-256 of the `body_size` body bytes. Verifying the signature + recomputing this hash transitively authenticates the body. |

The struct is `<4sIIIIIIBBBBIIQQQ32s`.

## Components

A component is a blob the boot stub decompresses to a flash address before the
firmware runs — the firmware itself, a second core's firmware, a radio blob. The
blobs sit in the slot after the ROMFS body; `body_sha256` does not cover them, each
carries its own hash, so `boot.py` hashes only the filesystem and the stub hashes
only what it installs. An image built without firmware packing lists none. Each
entry is 48 bytes, `<IIII32s`:

| Offset | Field | Type | Meaning |
|---|---|---|---|
| 0 | `dest` | `uint32` | The absolute flash address the decompressed bytes are written to. The stub refuses a destination outside the install window its board allows. |
| 4 | `offset` | `uint32` | Where the blob starts, relative to the slot start. It may move from release to release; the delta encoder follows it. |
| 8 | `size` | `uint32` | The blob's compressed size. |
| 12 | `installed_size` | `uint32` | Its decompressed size; `dest..dest+installed_size` must fit the install window. |
| 16 | `sha256` | `32s` | SHA-256 of the compressed blob — the bytes the stub verifies and then decompresses. |

Every blob is a raw LZMA1 stream with a 256 KiB dictionary; a different codec
would be a `header_version` bump.

## Certificate

A camera embeds only **root** public keys (`ROOT_KEYS`, from the project's
`keys/root_keys.json`: a primary and a backup). The key that signed the image is
introduced by this 160-byte record, signed by a root. New signing keys therefore
need no change on the camera, and a retired key is refused by its epoch.

| Offset | Field | Type | Meaning |
|---|---|---|---|
| 0 | `magic` | `4s` | `OMVC` |
| 4 | `version` | `uint32` | `1` |
| 8 | `kind` | `uint32` | `0` = an OTA signing key (releases), `1` = a factory key (factory images). Only a factory-signed image's first boot opens the camera's key-provisioning window, so this is a certified property, not a convention on key ids. |
| 12 | `root_id` | `uint32` | Which embedded root signed this certificate: `0` (primary) or `1` (backup). |
| 16 | `epoch` | `uint32` | Anti-revocation counter. Moves only when a key is retired; a camera that has confirmed a release at epoch *N* refuses certificates below *N* from then on. |
| 20 | `alg` | `int32` | COSE algorithm id of the certified key: `-7` (ES256). Certificates carry P-256 keys only. |
| 24 | `pubkey` | `65s` + 7 pad | The certified key, an uncompressed P-256 point (`04 ‖ X ‖ Y`). |
| 96 | `signature` | `64s` | The root's ECDSA `R ‖ S` over bytes 0..95. |

The struct is `<4sIIIIi65s7x64s`. The certified key's **id** — what `inspect`,
`verify` and the release ledger print — is derived from the key itself: the first
31 bits of the public point's SHA-256, never `0`.

## JSON metadata

After the certificate comes a length-delimited JSON blob (`meta_size` bytes),
serialised deterministically (`sort_keys=True`, compact separators, UTF-8). It is
inside the signed region — authenticated — but `boot.py` never parses it: the
trust path stays a tiny parser-free path.

This blob is a **verbatim copy of the image's `/rom/system.json`** — the same
board identity + provenance the build packs into the ROMFS body (see
[the projects page](../tutorial/02-projects.md#systemjson-generated-read-only)). The on-device app reads
its identity from `/rom/system.json` (one read path, OTA or not); the trailer
carries the copy so **host tools** — the update server, an `inspect` command — and
the bootloader can read it straight from the signed trailer without mounting the
ROMFS.

This is deliberate layering: the trailer is the **metadata envelope** and the
ROMFS body stays an **opaque payload**. Anything that routes, validates, or dumps
an image reads its identity and version from the signed envelope — never by
parsing the filesystem inside the body — the same separation MCUboot, SUIT, and
FIT use. Reading from the trailer is also the more trustworthy path: those bytes
are inside the signed region, so no ROMFS mount plus body-hash check is needed
first.

```json
{
  "product": "orchard-sentry",
  "board": "OPENMV_N6",
  "product_id": 5748986181262328784,
  "account_id": "acct_7bd21c50e83a94f1",
  "dev": false,
  "board_name": "OrchardSentry Pro",
  "app_version": "1.0.0",
  "vendor": "Acme Robotics",
  "ota": true,
  "firmware": {"version": "5.0.1", "commit": "9f2c1ab3d4e5f60718293a4b5c6d7e8f90a1b2c3"},
  "micropython": "1.29.0",
  "toolchain": {"mpy_cross": "1.29.0", "vela": "5.0.0", "stedgeai": "2.1.0", "sdk": "1.6.0"}
}
```

The trust-critical fields (`product_id`, `payload_version`, the certificate, …) are
also in the fixed part and enforced there; the JSON is provenance, not trust input.

## Signature algorithms

Algorithms are named by their IANA COSE identifier (RFC 9053), the same scheme the
host signer and the device verifier share. Certified signing keys are ECDSA over
P-256 with SHA-256 (ES256, `-7`): a 64-byte raw `R‖S` signature and a 65-byte
uncompressed public key. The registry also knows ES384 (`-35`) and ES512 (`-36`),
which the manifest signer may use; the trailer's certificate slot is sized for
P-256 and admits nothing else.

## How verification works

The body hash carried in the signed header is the hinge — you sign a small footer,
not the megabytes. On the device, `boot.py`:

1. reads the header, checks `magic`, `header_version` and `comp_count`, and locates
   the certificate and the signed region from the *authenticated* `comp_count` and
   `meta_size`;
2. checks the certificate's root signature against the root named by `root_id` in
   its baked-in `ROOT_KEYS` (an unknown root is rejected without verifying
   anything else), and the certificate's `epoch` against the floor it has
   confirmed;
3. verifies the image signature over the signed region with the certified key;
4. recomputes SHA-256 of the body and compares it to `body_sha256`;
5. enforces `product_id` and the anti-rollback rule — `payload_version` against the
   floor, or `publish_seq` on a `product_id = 0` camera — against each slot; it
   boots the newest one that passes.

The CRC is checked first as a cheap torn-write reject; it is not a trust check.
Trust starts and ends at the roots the firmware embeds — a key in the trailer is
trusted only through a certificate one of them signed. (`boot.py` and the mbedtls
verify shim are the layers above; this page documents the format they consume.)

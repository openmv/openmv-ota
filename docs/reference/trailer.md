# The signed trailer format

The **trailer** is the signed footer at the end of every OTA image. It carries a
SHA-256 of the image body, an ECDSA signature, the targeting and anti-rollback
fields a camera checks before mounting the body, and a JSON blob of build
provenance.

The tutorial covers both ends of its life: [release
artifacts](../tutorial/08-release-artifacts.md) is the host side (how `build`
signs it and `inspect`/`verify` read it back), and [boot and
rollback](../tutorial/11-boot-and-rollback.md) is the camera side (`boot.py`
verifying it at every boot). This page is the byte-level on-flash format those
two sides agree on; the codec —
[src/openmv_ota/ota/trailer.py](../../src/openmv_ota/ota/trailer.py) — is its
source of truth.

## Layout

The trailer occupies one 4 KiB control block on every OTA-capable board —
deliberately sized to 4 KiB rather than the flash erase block. Control data is
never erased independently of its slot, so a large-erase board doesn't inflate
the sector, and a board reporting a tiny sector (byte-writable MRAM) is floored
to the same 4 KiB: growing the metadata can never reshape the layout. Boards whose ROMFS is a single large internal-flash sector
(OpenMV2/3/4) carry the same trailer in single-image mode (see
[the OTA-projects page](../tutorial/04-ota-projects.md)). Laid out little-endian:

```
[ header (80) ][ json_meta (meta_size) ][ signature (sig_size) ][ crc32 (4) ]
└──── signed region: header ‖ meta ────┘
└────────── crc32 region: everything before the crc ───────────┘
```

- The **signed region** is exactly `header ‖ meta`. The signer signs those bytes;
  the verifier hashes the identical *stored* bytes (never a re-serialisation, so
  there is no JSON-canonicalisation pitfall). Every header field and all the JSON
  provenance are therefore authenticated.
- The **signature** and **crc32** necessarily sit outside the signed region. The
  CRC is torn-write detection only (a cheap pre-reject), not authenticity.
- `meta_size` and `sig_size` live *inside* the signed header, so the framing a
  verifier trusts comes from authenticated fields — never from the flexible blob.
- `build romfs` pads the trailer with `0xFF` to fill its 4 KiB block.

## Header fields

The fixed header is 80 bytes, all scalar fields 4-byte aligned. In order:

| Field | Type | Meaning |
|---|---|---|
| `magic` | `4s` | Payload kind + format marker: `OMVR` = ROMFS app, `OMVF` = firmware (reserved). The first cheap reject; folds the kind into the magic so there's no separate type field. |
| `header_version` | `uint32` | Layout version of *this fixed header* (`1`). `boot.py` hard-rejects an unknown version rather than mis-parse it. |
| `body_size` | `uint32` | Length of the ROMFS body before the trailer; bounds the mount and the body hash. |
| `pad_size` | `uint32` | Count of `0xFF` bytes between the body and the status/trailer sectors. `body_size + pad_size` = where the status sector begins, making the slot self-describing across boards with different erase geometry. |
| `meta_size` | `uint32` | Byte length of the JSON metadata blob. |
| `sig_size` | `uint32` | Byte length of the signature; must equal the algorithm's size. |
| `product_id` | `uint32` | Target product id; the cross-flash guard. The build auto-assigns a nonzero id, so this is `0` (check skipped) only if you override it to `0`. |
| `min_platform_version` | `uint32` | Minimum platform version the payload needs, encoded `(major<<24)\|(minor<<16)\|(patch<<8)\|build`. For a ROMFS app the platform is the OpenMV base firmware. `0` = no constraint. |
| `payload_version` | `uint32` | The app's `app_version` (from `settings.json`), encoded `(major<<24)\|(minor<<16)\|(patch<<8)` so versions compare as plain integers. It is the **anti-rollback input**: the installer and `boot.py` reject an image below the device's recorded floor, and `confirm()` raises that floor to this value. It never *orders* the slots — the install counter does — which is what keeps reinstalling the same version legal. |
| `payload_version_floor` | `uint32` | A **build-declared floor**: stamped from an optional `rollback_floor` in `settings.json` (same encoding; must be `<= payload_version`, `0` = unset), letting a release state "never accept anything older than X" in its signed header. Today only host tooling reads it (`build inspect` reports it as `rollback_floor`); devices ignore it, because the device's own floor — risen by `confirm()` — already refuses anything older. |
| `key_id` | `uint32` | Which trusted key signed; a selector into the device's baked-in key table, not trust itself. |
| `sig_alg` | `int32` | COSE algorithm id (negative — hence signed); authenticated, so the algorithm can't be downgraded. |
| `body_sha256` | `32s` | SHA-256 of the `body_size` body bytes. Verifying the signature + recomputing this hash transitively authenticates the body. |

The single signed (`int32`) field, `sig_alg`, sits just before the digest so the
struct's lone `i` stays isolated at the end of the long `uint32` run.

## JSON metadata

After the header comes a length-delimited JSON blob (`meta_size` bytes),
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
  "product_id": 2937722637,
  "board_name": "OrchardSentry Pro",
  "app_version": "1.0.0",
  "vendor": "Acme Robotics",
  "ota": true,
  "firmware": {"version": "5.0.0", "commit": "9f2c1ab3d4e5f60718293a4b5c6d7e8f90a1b2c3"},
  "micropython": "1.28.0",
  "toolchain": {"mpy_cross": "1.28.0", "vela": "3.12.0", "stedgeai": "2.1.0", "sdk": "1.6.0"}
}
```

The trust-critical fields (`product_id`, `payload_version`, `sig_alg`, …) are also
in the fixed header and enforced there; the JSON is provenance, not trust input.

## Signature algorithms

Algorithms are named by their IANA COSE identifier (RFC 9053), the same scheme the
host signer and the device verifier share. The set is ECDSA over the NIST P-curves
(SHA-256/384/512) — exactly what the OpenMV firmware's mbedtls compiles in and the
host signs with. Signatures are stored as fixed-width raw `R‖S` (COSE/JOSE
convention).

| COSE id | Name | Curve | Hash | Signature | Public key |
|---|---|---|---|---|---|
| `-7` | ES256 | secp256r1 | SHA-256 | 64 | 65 |
| `-35` | ES384 | secp384r1 | SHA-384 | 96 | 97 |
| `-36` | ES512 | secp521r1 | SHA-512 | 132 | 133 |

`project new --ota --sig-alg ES256` (the default) provisions P-256 keys; ES384 /
ES512 raise the curve and the signature/key sizes accordingly. Any COSE id outside
this set is rejected — supporting another curve means wiring it on both the host
and the device first.

## How verification works

The body hash carried in the signed header is the hinge — you sign a small footer,
not the megabytes. On the device, `boot.py`:

1. reads the header, checks `magic` and `header_version`, and recomputes the
   signed region `data[0 : 80 + meta_size]` from the *authenticated* `meta_size`;
2. looks `key_id` up in its baked-in `TRUSTED_KEYS` (an absent/revoked id is
   rejected without verifying), and reads `sig_alg` for the curve + hash;
3. verifies the signature over the signed region;
4. recomputes SHA-256 of the body and compares it to `body_sha256`;
5. enforces `product_id`, `min_platform_version`, and the anti-rollback rule against
   each slot; it boots the newest one that passes.

The CRC is checked first as a cheap torn-write reject; it is not a trust check.
The trusted public keys come only from the firmware's baked-in set — an embedded
public key is never trusted. (`boot.py` and the mbedtls verify shim are the layers
above; this page documents the format they consume.)

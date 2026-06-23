# trailer

The **trailer** is the signed footer that `openmv-ota build romfs` stamps onto an
OTA image. On its own, a `build romfs` body is just a bare ROMFS filesystem — a
camera would mount and run whatever is in it. The trailer is what makes an image
*trustworthy*: it lets the camera confirm, before running an image, that the image
is authentic, intact, meant for this product, and not an old version rolled back.

It does that by authenticating a large image with a small signed footer. The
trailer carries a SHA-256 of the body, an ECDSA signature over the footer, the
targeting and anti-rollback fields the camera checks, and a JSON blob of build
provenance the app can read. Verifying the signature and re-hashing the body
together authenticate the whole image (see
[How verification works](#how-verification-works)).

On the camera, that checking is the job of **`boot.py`** — a small, fixed startup
script compiled into the firmware (not user-editable). At boot it chooses which
image slot to mount and verifies that slot's trailer first, falling back to the
golden image if the check fails. It is kept deliberately tiny and parser-free on
the trust path. The host signer (`build romfs`) and `boot.py` agree on exactly the
format described here.

This page is that on-flash format; the codec —
[src/openmv_ota/ota/trailer.py](../src/openmv_ota/ota/trailer.py) — is its source
of truth. (`boot.py`, the slot layout, and the on-device verifier are later layers
that consume this format; the [architecture](architecture.md) page sketches the
whole picture.)

## Layout

The trailer occupies one 4 KiB flash sector (`TRAILER_SZ = 4096`), laid out
little-endian:

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
- `build romfs` pads the trailer with `0xFF` to fill the 4 KiB sector.

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
| `board_id` | `uint32` | Target product id; the cross-flash guard. The build auto-assigns a nonzero id, so this is `0` (check skipped) only if you override it to `0`. |
| `min_platform_version` | `uint32` | Minimum platform version the payload needs, encoded `(major<<24)\|(minor<<16)\|(patch<<8)\|build`. For a ROMFS app the platform is the OpenMV base firmware. `0` = no constraint. |
| `payload_version` | `uint32` | The monotonic anti-rollback counter / OTA epoch. `boot.py` rejects a FRONT older than BACK. Distinct from any human version string. |
| `payload_version_floor` | `uint32` | A forward rollback floor this image asserts: every later update must be `>=` it. Set from `settings.json`'s `rollback_floor` (encoded like `payload_version`); `0` = no extra floor. |
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
[project.md](project.md#systemjson-generated-read-only)). The on-device app reads
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
  "board_id": 4097,
  "board_name": "OrchardSentry Pro",
  "app_version": "2.3.0",
  "vendor": "Acme Robotics",
  "ota": true,
  "firmware": {"version": "5.0.0", "commit": "9f2c1ab3d4e5f60718293a4b5c6d7e8f90a1b2c3"},
  "micropython": "1.28.0",
  "toolchain": {"mpy_cross": "1.28.0", "vela": "3.12.0", "stedgeai": "2.1.0", "sdk": "1.6.0"}
}
```

The trust-critical fields (`board_id`, `payload_version`, `sig_alg`, …) are also
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
5. enforces `board_id`, `min_platform_version`, and the anti-rollback rule against
   BACK; on any FRONT failure it falls back to the golden BACK image.

The CRC is checked first as a cheap torn-write reject; it is not a trust check.
The trusted public keys come only from the firmware's baked-in set — an embedded
public key is never trusted. (`boot.py` and the mbedtls verify shim are later
layers; this page documents the format they consume.)

## See also

- [build.md](build.md) — `build romfs` produces the trailer from a project.
- [project.md](project.md) — `project new --ota` provisions the keys and identity
  the trailer records.

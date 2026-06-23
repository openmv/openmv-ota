"""OTA trailer codec — the signed on-flash trailer for OpenMV OTA images.

A ``build romfs`` body is a bare ``VfsRom`` container; the device's frozen
``boot.py`` only mounts a *trailer*-stamped slot. This module is the format
source-of-truth for that trailer: a small fixed trust-header, a length-delimited
JSON metadata blob, the signature, and a CRC. It is pure and does no crypto — the
signature is opaque bytes produced/verified by a separate layer.

Layout (little-endian)::

    [ header (HEADER_SIZE) ][ json_meta (meta_size) ][ signature (sig_size) ][ crc32 ]
    \\______ signed region: header || meta ______/
    \\____________ crc32 region: everything before the crc ____________/

The **signed region** is exactly ``header || meta`` — the signer signs those bytes
and the verifier hashes the identical stored bytes (never a re-serialisation, so
there is no JSON-canonicalisation pitfall). The signature and CRC necessarily sit
outside it. ``meta_size``/``sig_size`` live in the signed header, so the framing a
verifier trusts comes from authenticated fields, not from the flexible blob.

The fixed header, in order (see the plan for per-field semantics)::

    magic(4s) header_version body_size pad_size meta_size sig_size
    board_id min_platform_version payload_version payload_version_floor
    key_id sig_alg(int32) body_sha256(32s)

``magic`` doubles as the payload-kind discriminator (``OMVR`` = ROMFS app,
``OMVF`` = firmware, reserved). The lone signed field ``sig_alg`` is placed just
before the final digest so the struct's single ``i`` stays at the end.
"""

from __future__ import annotations

import binascii
import json
import struct
from dataclasses import dataclass

from .algorithms import AlgSpec, algorithm_for
from .errors import OtaError

# --- Format constants -------------------------------------------------------

MAGIC_ROMFS_APP = b"OMVR"   # ROMFS application image
MAGIC_FIRMWARE = b"OMVF"    # firmware image (reserved; a future payload kind)

HEADER_VERSION = 1
# Maximum packed trailer size. The trailer is padded out to one flash erase block
# by the build (4 KiB on every OTA-capable board; see openmv_ota.ota.geometry), so
# this caps the trailer content at the smallest such block, guaranteeing it fits in
# one block on any board. It is NOT a fixed on-flash size: the padded sector is the
# board's erase block, which is >= this on boards with larger blocks.
TRAILER_SZ = 4096
CRC_SIZE = 4

# Fixed trust-header. The single signed field (sig_alg) is the lone "i".
HEADER_STRUCT = "<4sIIIIIIIIIIi32s"
HEADER_SIZE = struct.calcsize(HEADER_STRUCT)            # 80
_META_SIZE_OFFSET = struct.calcsize("<4sIII")           # magic, version, body, pad => 16


@dataclass
class Trailer:
    """The decoded trailer fields. ``meta_size``/``sig_size`` are not stored — they
    are derived from ``meta``/``signature``; the payload kind is the magic."""

    body_size: int
    pad_size: int
    meta: dict
    board_id: int
    min_platform_version: int
    payload_version: int
    payload_version_floor: int
    key_id: int
    sig_alg: int
    body_sha256: bytes
    signature: bytes = b""
    header_version: int = HEADER_VERSION


def _serialize_meta(meta: dict) -> bytes:
    """Deterministic JSON: sorted keys, compact separators, UTF-8. Reproducible for
    builds; correctness relies on signing the stored bytes, not canonicalisation."""
    return json.dumps(meta, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _build_signed_region(t: Trailer) -> tuple[bytes, AlgSpec]:
    """Pack ``header || meta`` (the signed region) for a trailer, validating the
    fields that ``struct`` would silently pad/truncate."""
    spec = algorithm_for(t.sig_alg)
    if len(t.body_sha256) != 32:
        raise OtaError("body_sha256 must be 32 bytes, got %d" % len(t.body_sha256))
    meta_bytes = _serialize_meta(t.meta)
    header = struct.pack(
        HEADER_STRUCT,
        MAGIC_ROMFS_APP,
        t.header_version,
        t.body_size,
        t.pad_size,
        len(meta_bytes),
        spec.sig_size,
        t.board_id,
        t.min_platform_version,
        t.payload_version,
        t.payload_version_floor,
        t.key_id,
        t.sig_alg,
        t.body_sha256,
    )
    return header + meta_bytes, spec


def signed_region(data) -> bytes:
    """The exact bytes a signer signs / a verifier hashes: ``header || meta``.

    Given a :class:`Trailer`, build that region (with ``sig_size`` from the
    algorithm but no signature bytes yet) — this is the signer's input. Given raw
    trailer bytes, slice ``[0 : HEADER_SIZE + meta_size]`` using the ``meta_size``
    read from the fixed header (bounds-checked before slicing).
    """
    if isinstance(data, Trailer):
        region, _spec = _build_signed_region(data)
        return region
    if len(data) < HEADER_SIZE:
        raise OtaError("trailer too small: %d < %d header bytes" % (len(data), HEADER_SIZE))
    meta_size = struct.unpack_from("<I", data, _META_SIZE_OFFSET)[0]
    end = HEADER_SIZE + meta_size
    if end > len(data):
        raise OtaError("signed region [0:%d] exceeds %d bytes" % (end, len(data)))
    return bytes(data[:end])


def pack_trailer(t: Trailer) -> bytes:
    """Serialise a complete trailer: ``header || meta || signature || crc32``.

    The signature must already be set and match the algorithm's size (build flow:
    construct a ``Trailer`` with ``signature=b""``, sign ``signed_region(t)``, set
    ``t.signature``, then pack).
    """
    region, spec = _build_signed_region(t)
    if len(t.signature) != spec.sig_size:
        raise OtaError(
            "signature must be %d bytes for %s, got %d"
            % (spec.sig_size, spec.name, len(t.signature))
        )
    body = region + t.signature
    crc = binascii.crc32(body) & 0xFFFFFFFF
    out = body + struct.pack("<I", crc)
    if len(out) > TRAILER_SZ:
        raise OtaError(
            "trailer is %d bytes, over the %d-byte limit (one erase block)"
            % (len(out), TRAILER_SZ))
    return out


def parse_trailer(data: bytes) -> Trailer:
    """Decode and validate a trailer. Raises ``OtaError`` on any malformation —
    bad magic, unknown ``header_version``, an unsupported algorithm, a
    ``sig_size`` that disagrees with the algorithm, truncation, or a bad CRC."""
    if len(data) < HEADER_SIZE:
        raise OtaError("trailer too small: %d < %d header bytes" % (len(data), HEADER_SIZE))
    (
        magic,
        header_version,
        body_size,
        pad_size,
        meta_size,
        sig_size,
        board_id,
        min_platform_version,
        payload_version,
        payload_version_floor,
        key_id,
        sig_alg,
        body_sha256,
    ) = struct.unpack_from(HEADER_STRUCT, data, 0)

    if magic != MAGIC_ROMFS_APP:
        raise OtaError("bad magic %r (expected %r)" % (magic, MAGIC_ROMFS_APP))
    if header_version != HEADER_VERSION:
        raise OtaError(
            "unsupported header_version %d (this codec writes %d)"
            % (header_version, HEADER_VERSION)
        )
    spec = algorithm_for(sig_alg)
    if sig_size != spec.sig_size:
        raise OtaError("sig_size %d != %d for %s" % (sig_size, spec.sig_size, spec.name))

    body_end = HEADER_SIZE + meta_size + sig_size
    if body_end + CRC_SIZE > len(data):
        raise OtaError("trailer truncated: needs %d bytes, have %d" % (body_end + CRC_SIZE, len(data)))

    crc_stored = struct.unpack_from("<I", data, body_end)[0]
    if (binascii.crc32(data[:body_end]) & 0xFFFFFFFF) != crc_stored:
        raise OtaError("crc32 mismatch (corrupt trailer)")

    meta_bytes = data[HEADER_SIZE : HEADER_SIZE + meta_size]
    signature = data[HEADER_SIZE + meta_size : body_end]
    try:
        meta = json.loads(meta_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise OtaError("meta is not valid JSON: %s" % e) from None

    return Trailer(
        body_size=body_size,
        pad_size=pad_size,
        meta=meta,
        board_id=board_id,
        min_platform_version=min_platform_version,
        payload_version=payload_version,
        payload_version_floor=payload_version_floor,
        key_id=key_id,
        sig_alg=sig_alg,
        body_sha256=bytes(body_sha256),
        signature=bytes(signature),
        header_version=header_version,
    )

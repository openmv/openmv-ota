"""OTA trailer codec — the signed on-flash trailer for OpenMV OTA images.

A ``build romfs`` body is a bare ``VfsRom`` container; the device's frozen ``boot.py``
only mounts a *trailer*-stamped slot, and the boot stub installs the firmware the slot
carries. This module is the format source-of-truth for that trailer: a fixed trust
header, a table of **components** (blobs appended after the filesystem that the stub
decompresses to flash addresses), the **certificate** of the key that signed the image,
a length-delimited JSON metadata blob, the signature, and a CRC. It is pure and does no
crypto — the signature is opaque bytes produced/verified by a separate layer.

Layout (little-endian)::

    [ header (96) ][ components (n x 48) ][ certificate (160) ][ json_meta ][ signature ][ crc32 ]
    \\_________________________ signed region _________________________/
    \\____________________ crc32 region: everything before the crc ________________/

The **signed region** is everything before the signature — the signer signs those bytes
and the verifier hashes the identical stored bytes (never a re-serialisation, so there is
no JSON-canonicalisation pitfall). ``comp_count`` and ``meta_size`` live in the signed
header, so the framing a verifier trusts comes from authenticated fields. The signature's
algorithm and length come from the certificate (``alg``), which the verifier reads first.

The fixed header, 96 bytes in four groups (``docs/reference/trailer.md`` has per-field
semantics)::

    framing     magic(4s)=OMVR header_version body_size comp_count meta_size
    versions    min_stub payload_version
    stub        uart_en(u8) uart_id(u8) wdt_en(u8) wdt_id(u8) baud wdt_timeout_ms
    identity    product_id(u64) publish_seq(u64) reserved(u64)
    digest      body_sha256(32s)

``body_sha256`` covers the filesystem image only (what ``boot.py`` mounts); each component
carries its own hash, so nothing is hashed twice. ``product_id`` is 64-bit (a 32-bit id
derived from a hash of the product name collides at a few thousand products).
``publish_seq`` is the account's publish counter, the anti-rollback key for a camera built
with ``PRODUCT_ID = 0``; every other camera orders by ``payload_version``.

**``header_version`` is a one-shot.** Both this codec and the device check it for exact
equality and refuse anything else, and the boot stub that reads it is flashed over USB
only — so a bump makes every camera in the field reject every new image until it is
reflashed. Version 4 is the stub format; ``reserved`` is the headroom the next field comes
out of.
"""

from __future__ import annotations

import binascii
import json
import struct
from dataclasses import dataclass, field

from . import certificate as certificate_mod
from .algorithms import AlgSpec, algorithm_for
from .errors import OtaError

# --- Format constants -------------------------------------------------------

MAGIC_ROMFS_APP = b"OMVR"   # ROMFS application image
MAGIC_FIRMWARE = b"OMVF"    # firmware image (reserved; unused since v4 carries firmware as a component)

HEADER_VERSION = 4
# Maximum packed trailer size == the on-flash trailer sector: the build pads the
# trailer with 0xFF to exactly one control block, and control_block() is 4 KiB on
# every board -- deliberately NOT the erase block (see openmv_ota.ota.geometry), so
# growing the metadata on a large-erase board can't reshape the slot layout.
TRAILER_SZ = 4096
CRC_SIZE = 4

HEADER_STRUCT = "<4sIIIIIIBBBBIIQQQ32s"
HEADER_SIZE = struct.calcsize(HEADER_STRUCT)            # 96
_COMP_COUNT_OFFSET = struct.calcsize("<4sII")           # magic, version, body_size => 12
_META_SIZE_OFFSET = struct.calcsize("<4sIII")           # ... comp_count => 16

COMPONENT_STRUCT = "<IIII32s32s"                 # dest offset size installed_size sha256 installed_sha256
COMPONENT_SIZE = struct.calcsize(COMPONENT_STRUCT)      # 80
# The most components a trailer may list. The device reads comp_count before it has
# verified anything, so it has to be a bound, not a budget: 16 is far past any board
# (the AE3 needs 2) and keeps the framing arithmetic inside one 4 KiB block.
MAX_COMPONENTS = 16

CERT_SIZE = certificate_mod.SIZE


@dataclass
class Component:
    """One blob the boot stub installs: ``size`` compressed bytes at ``offset`` in the slot,
    decompressed to ``installed_size`` bytes at flash address ``dest``. ``sha256`` is over
    the compressed bytes -- the ones the stub verifies and then decompresses;
    ``installed_sha256`` over the decompressed bytes -- what the stub checks the decoder's
    output against, and what its install record then names."""

    dest: int
    offset: int
    size: int
    installed_size: int
    sha256: bytes
    installed_sha256: bytes


@dataclass
class Trailer:
    """The decoded trailer fields. ``meta_size``/``comp_count`` are not stored — they are
    derived from ``meta``/``components``; the payload kind is the magic. ``certificate`` is
    the packed 160-byte certificate of the signing key (see :mod:`.certificate`); the
    signature's algorithm is the certificate's."""

    body_size: int
    meta: dict
    product_id: int
    payload_version: int
    body_sha256: bytes
    certificate: bytes
    components: list = field(default_factory=list)
    min_stub: int = 0
    uart_en: int = 0
    uart_id: int = 0
    wdt_en: int = 0
    wdt_id: int = 0
    baud: int = 0
    wdt_timeout_ms: int = 0
    publish_seq: int = 0
    """The account's publish counter. 0 when the project does not use one, which is
    every project that is not a platform minting images for ``PRODUCT_ID = 0``
    cameras -- the field is in the header for all of them, and ignored by all of them."""
    reserved: int = 0
    signature: bytes = b""
    header_version: int = HEADER_VERSION

    @property
    def cert(self) -> certificate_mod.Certificate:
        """The parsed certificate (structurally valid; trust needs :func:`certificate.verify`)."""
        return certificate_mod.parse(self.certificate)

    @property
    def sig_alg(self) -> int:
        """The image signature's COSE algorithm: the certified key's."""
        return self.cert.alg

    @property
    def key_id(self) -> int:
        """The signing key's id, derived from its public key (:func:`certificate.key_id_of`)."""
        return self.cert.key_id


def _serialize_meta(meta: dict) -> bytes:
    """Deterministic JSON: sorted keys, compact separators, UTF-8. Reproducible for
    builds; correctness relies on signing the stored bytes, not canonicalisation."""
    return json.dumps(meta, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _pack_component(c: Component) -> bytes:
    if len(c.sha256) != 32:
        raise OtaError("component sha256 must be 32 bytes, got %d" % len(c.sha256))
    if len(c.installed_sha256) != 32:
        raise OtaError("component installed_sha256 must be 32 bytes, got %d"
                       % len(c.installed_sha256))
    return struct.pack(COMPONENT_STRUCT, c.dest, c.offset, c.size, c.installed_size, c.sha256,
                       c.installed_sha256)


def _build_signed_region(t: Trailer) -> tuple[bytes, AlgSpec]:
    """Pack the signed region for a trailer, validating the fields that ``struct`` would
    silently pad/truncate."""
    if len(t.body_sha256) != 32:
        raise OtaError("body_sha256 must be 32 bytes, got %d" % len(t.body_sha256))
    if len(t.components) > MAX_COMPONENTS:
        raise OtaError("%d components, over the %d-component limit"
                       % (len(t.components), MAX_COMPONENTS))
    if len(t.certificate) != CERT_SIZE:
        raise OtaError("certificate must be %d bytes, got %d" % (CERT_SIZE, len(t.certificate)))
    spec = algorithm_for(certificate_mod.parse(t.certificate).alg)
    meta_bytes = _serialize_meta(t.meta)
    header = struct.pack(
        HEADER_STRUCT,
        MAGIC_ROMFS_APP,
        t.header_version,
        t.body_size,
        len(t.components),
        len(meta_bytes),
        t.min_stub,
        t.payload_version,
        t.uart_en, t.uart_id, t.wdt_en, t.wdt_id,
        t.baud,
        t.wdt_timeout_ms,
        t.product_id,
        t.publish_seq,
        t.reserved,
        t.body_sha256,
    )
    comps = b"".join(_pack_component(c) for c in t.components)
    return header + comps + bytes(t.certificate) + meta_bytes, spec


def _framing(data) -> tuple[int, int, int]:
    """``(comp_count, cert_offset, meta_end)`` read off a raw trailer's header, bounds-checked
    against ``data``. The authenticated framing every reader trusts."""
    if len(data) < HEADER_SIZE:
        raise OtaError("trailer too small: %d < %d header bytes" % (len(data), HEADER_SIZE))
    comp_count = struct.unpack_from("<I", data, _COMP_COUNT_OFFSET)[0]
    if comp_count > MAX_COMPONENTS:
        raise OtaError("comp_count %d is over the %d-component limit" % (comp_count, MAX_COMPONENTS))
    meta_size = struct.unpack_from("<I", data, _META_SIZE_OFFSET)[0]
    cert_off = HEADER_SIZE + comp_count * COMPONENT_SIZE
    end = cert_off + CERT_SIZE + meta_size
    if end > len(data):
        raise OtaError("signed region [0:%d] exceeds %d bytes" % (end, len(data)))
    return comp_count, cert_off, end


def signed_region(data) -> bytes:
    """The exact bytes a signer signs / a verifier hashes: everything before the signature.

    Given a :class:`Trailer`, build that region (no signature bytes yet) — this is the
    signer's input. Given raw trailer bytes, slice it using the ``comp_count`` and
    ``meta_size`` read from the fixed header (bounds-checked before slicing).
    """
    if isinstance(data, Trailer):
        region, _spec = _build_signed_region(data)
        return region
    _n, _cert_off, end = _framing(data)
    return bytes(data[:end])


def pack_trailer(t: Trailer) -> bytes:
    """Serialise a complete trailer: ``signed region || signature || crc32``.

    The signature must already be set and match the certificate's algorithm (build flow:
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
    bad magic, unknown ``header_version``, too many components, a malformed
    certificate, truncation, or a bad CRC. The fields are trustworthy only once the
    certificate and the signature have been verified."""
    comp_count, cert_off, meta_end = _framing(data)
    (
        magic, header_version, body_size, _comp_count, meta_size, min_stub, payload_version,
        uart_en, uart_id, wdt_en, wdt_id, baud, wdt_timeout_ms,
        product_id, publish_seq, reserved, body_sha256,
    ) = struct.unpack_from(HEADER_STRUCT, data, 0)

    if magic != MAGIC_ROMFS_APP:
        raise OtaError("bad magic %r (expected %r)" % (magic, MAGIC_ROMFS_APP))
    if header_version != HEADER_VERSION:
        raise OtaError(
            "unsupported header_version %d (this codec writes %d)"
            % (header_version, HEADER_VERSION)
        )
    certificate = bytes(data[cert_off:cert_off + CERT_SIZE])
    spec = algorithm_for(certificate_mod.parse(certificate).alg)

    body_end = meta_end + spec.sig_size
    if body_end + CRC_SIZE > len(data):
        raise OtaError("trailer truncated: needs %d bytes, have %d" % (body_end + CRC_SIZE, len(data)))

    crc_stored = struct.unpack_from("<I", data, body_end)[0]
    if (binascii.crc32(data[:body_end]) & 0xFFFFFFFF) != crc_stored:
        raise OtaError("crc32 mismatch (corrupt trailer)")

    components = [
        Component(*struct.unpack_from(COMPONENT_STRUCT, data, HEADER_SIZE + i * COMPONENT_SIZE))
        for i in range(comp_count)
    ]
    for c in components:
        c.sha256, c.installed_sha256 = bytes(c.sha256), bytes(c.installed_sha256)
    meta_bytes = data[cert_off + CERT_SIZE : meta_end]
    signature = data[meta_end:body_end]
    try:
        meta = json.loads(meta_bytes)
    except (ValueError, RecursionError) as e:   # JSONDecodeError/UnicodeDecodeError; deep nesting
        raise OtaError("meta is not valid JSON: %s" % e) from None
    if not isinstance(meta, dict):
        # Every reader treats meta as a mapping (``meta.get(...)``); a bare list or number is a
        # malformed trailer, not a crash three calls later.
        raise OtaError("meta is not a JSON object")

    return Trailer(
        body_size=body_size,
        meta=meta,
        product_id=product_id,
        payload_version=payload_version,
        body_sha256=bytes(body_sha256),
        certificate=certificate,
        components=components,
        min_stub=min_stub,
        uart_en=uart_en, uart_id=uart_id, wdt_en=wdt_en, wdt_id=wdt_id,
        baud=baud, wdt_timeout_ms=wdt_timeout_ms,
        publish_seq=publish_seq,
        reserved=reserved,
        signature=bytes(signature),
        header_version=header_version,
    )

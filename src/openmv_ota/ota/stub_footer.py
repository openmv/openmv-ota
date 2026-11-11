"""The boot stub's image footer: a signature over the stub, at the end of its sector.

The stub is flashed over USB and never updates itself, so its formats are permanent and
nothing in the field verifies it today. The footer exists for the bootloader that will: it
can check the stub under the project's root without a stub change. ``build firmware`` and
``build stub`` seal every ``<board>-stub.bin`` this way, so the file is the stub's whole
sector::

    [ stub code (image_size) ][ 0xFF pad ][ footer (512) ]
                                           ^ sector end - 512

Footer, little-endian, 512 bytes::

    [ header (32) ][ image_sha256 (32) ][ certificate (160) ][ signature (64) ][ crc32 (4) ][ 0xFF ]
    \\____________________ signed region (224) ____________________/

Header: ``magic`` ``OMVS``, ``version`` 1, ``image_size`` (bytes of stub code from the sector
start), ``stub_version`` (the stub's ``STUB_VERSION``, the value its install record carries),
``product_id`` (the cross-flash guard the stub was built for) and a reserved u64 of zero.
The certificate is the signing key's (:mod:`openmv_ota.ota.certificate`), the signature the
certified key's raw ``R || S`` over the signed region -- the same two-step check an image
trailer gets: root over the certificate, certified key over the region, hash over the code.
"""

from __future__ import annotations

import binascii
import hashlib
import struct
from dataclasses import dataclass

from . import certificate as certificate_mod
from .algorithms import algorithm_for
from .errors import OtaError
from .keys import public_key_from_hex
from .sign import verify_region

MAGIC = b"OMVS"
VERSION = 1
SIZE = 512
_HEADER = "<4sIIIQQ"
HEADER_SIZE = struct.calcsize(_HEADER)                 # 32
SIGNATURE_SIZE = 64
SIGNED_SIZE = HEADER_SIZE + 32 + certificate_mod.SIZE   # 224
_CRC_OFFSET = SIGNED_SIZE + SIGNATURE_SIZE              # 288


@dataclass
class StubFooter:
    image_size: int
    stub_version: int
    product_id: int
    image_sha256: bytes
    certificate: bytes
    signature: bytes = b""


def _header(f: StubFooter) -> bytes:
    return struct.pack(_HEADER, MAGIC, VERSION, f.image_size, f.stub_version, f.product_id, 0)


def signed_region(f: StubFooter) -> bytes:
    """The bytes the certified key signs: header, code hash, certificate."""
    return _header(f) + f.image_sha256 + f.certificate


def pack(f: StubFooter) -> bytes:
    """Serialise a complete footer (``signature`` set), padded with 0xFF to :data:`SIZE`."""
    if len(f.image_sha256) != 32:
        raise OtaError("stub footer: image hash must be 32 bytes")
    if len(f.certificate) != certificate_mod.SIZE:
        raise OtaError("stub footer: certificate must be %d bytes" % certificate_mod.SIZE)
    if len(f.signature) != SIGNATURE_SIZE:
        raise OtaError("stub footer: signature must be %d bytes" % SIGNATURE_SIZE)
    body = signed_region(f) + f.signature
    out = body + struct.pack("<I", binascii.crc32(body) & 0xFFFFFFFF)
    return out + b"\xff" * (SIZE - len(out))


def parse(data: bytes) -> StubFooter:
    """Decode a footer (the sector's last :data:`SIZE` bytes). Raises :class:`OtaError` on a
    bad magic, version or crc."""
    data = bytes(data)
    if len(data) < SIZE:
        raise OtaError("stub footer: %d bytes, need %d" % (len(data), SIZE))
    magic, version, image_size, stub_version, product_id, reserved = struct.unpack_from(_HEADER, data)
    if magic != MAGIC:
        raise OtaError("stub footer: bad magic %r" % magic)
    if version != VERSION:
        raise OtaError("stub footer: unsupported version %d" % version)
    crc_stored, = struct.unpack_from("<I", data, _CRC_OFFSET)
    if (binascii.crc32(data[:_CRC_OFFSET]) & 0xFFFFFFFF) != crc_stored:
        raise OtaError("stub footer: crc32 mismatch (corrupt footer)")
    if reserved != 0:
        raise OtaError("stub footer: reserved field is not zero")
    return StubFooter(image_size, stub_version, product_id,
                      data[HEADER_SIZE:HEADER_SIZE + 32],
                      data[HEADER_SIZE + 32:SIGNED_SIZE],
                      data[SIGNED_SIZE:_CRC_OFFSET])


def seal(image: bytes, sector_size: int, *, stub_version: int, product_id: int,
         certificate: bytes, signer) -> bytes:
    """``image || 0xFF pad || footer``, exactly ``sector_size`` bytes: the stub's sector as
    flashed. ``signer`` is a :class:`openmv_ota.ota.signer.Signer` for the certified key."""
    room = sector_size - SIZE
    if len(image) > room:
        raise OtaError("stub is %d bytes but its sector holds %d after the %d-byte footer"
                       % (len(image), room, SIZE))
    f = StubFooter(len(image), stub_version, product_id, hashlib.sha256(image).digest(),
                   bytes(certificate))
    f.signature = signer.sign(signed_region(f))
    return bytes(image) + b"\xff" * (room - len(image)) + pack(f)


def footer_of(sector: bytes) -> bytes:
    """The footer bytes of a sealed stub sector."""
    if len(sector) < SIZE:
        raise OtaError("stub image is %d bytes, too short to carry a footer" % len(sector))
    return bytes(sector[-SIZE:])


def verify(sector: bytes, roots) -> tuple[bool, str]:
    """Verify a sealed stub sector against the project's ``roots`` (a ``RootKey`` list or
    ``{root_id: pubkey bytes}``), the way a bootloader would: root over the certificate,
    certified key over the footer, the code hashed. Returns ``(ok, reason)``."""
    from .verify import _roots_map
    try:
        f = parse(footer_of(sector))
    except OtaError as e:
        return False, "invalid stub footer: %s" % e
    try:
        cert = certificate_mod.verify(f.certificate, _roots_map(roots))
    except OtaError as e:
        return False, "untrusted certificate: %s" % e
    alg = algorithm_for(cert.alg)
    pub = public_key_from_hex(cert.pubkey.hex(), alg)
    if not verify_region(pub, signed_region(f), f.signature, alg):
        return False, "signature does not verify against key 0x%08x" % cert.key_id
    if f.image_size > len(sector) - SIZE:
        return False, "footer names %d bytes of code, the sector holds %d" % (
            f.image_size, len(sector) - SIZE)
    if hashlib.sha256(sector[:f.image_size]).digest() != f.image_sha256:
        return False, "stub code hash mismatch (tampered, or the wrong footer for this stub)"
    return True, "stub v%d for product 0x%016x, %d bytes, signed by key 0x%08x (%s, root %d, epoch %d)" % (
        f.stub_version, f.product_id, f.image_size, cert.key_id, alg.name, cert.root_id, cert.epoch)

"""The boot stub's signed footer (openmv_ota.ota.stub_footer): format, seal, verify."""

from __future__ import annotations

import hashlib
import struct

import pytest

from openmv_ota.ota import certificate as C
from openmv_ota.ota import keys, sign, stub_footer as F
from openmv_ota.ota.errors import OtaError
from tests import trust


class _Signer:
    """The Signer seam the build hands in: a local key with its root-issued certificate."""

    def __init__(self, priv, *, kind=C.KIND_FACTORY):
        self.priv, self.key_id = priv, C.key_id_of(trust.pub_bytes(priv))
        self.certificate = trust.certificate(priv, kind=kind)

    def sign(self, region):
        return sign.sign_region(self.priv, region, trust.SPEC)


@pytest.fixture
def signer():
    return _Signer(keys.generate_private_key(trust.SPEC))


def test_the_footer_layout_is_pinned():
    assert (F.HEADER_SIZE, F.SIGNED_SIZE, F.SIGNATURE_SIZE, F.SIZE) == (32, 224, 64, 512)
    assert F._CRC_OFFSET == 288 and struct.calcsize(F._HEADER) == 32


def test_seal_pads_the_code_to_the_sector_and_verifies(signer):
    code = bytes(range(256)) * 10
    sector = F.seal(code, 0x10000, stub_version=1, product_id=0xABCDEF0123456789,
                    certificate=signer.certificate, signer=signer)
    assert len(sector) == 0x10000
    assert sector[:len(code)] == code
    assert set(sector[len(code):-F.SIZE]) == {0xFF}
    f = F.parse(F.footer_of(sector))
    assert (f.image_size, f.stub_version, f.product_id) == (len(code), 1, 0xABCDEF0123456789)
    assert f.image_sha256 == hashlib.sha256(code).digest()
    assert f.certificate == signer.certificate and len(f.signature) == 64
    ok, why = F.verify(sector, trust.ROOTS)
    assert ok, why
    assert "stub v1" in why and "0xabcdef0123456789" in why and "key 0x%08x" % signer.key_id in why
    assert F.verify(sector, trust.root_keys())[0]               # a RootKey list works too


def test_seal_refuses_code_that_leaves_no_room_for_the_footer(signer):
    with pytest.raises(OtaError, match="holds 3584 after the 512-byte footer"):
        F.seal(b"x" * 3585, 0x1000, stub_version=1, product_id=1,
               certificate=signer.certificate, signer=signer)


def test_pack_checks_its_field_sizes(signer):
    f = F.StubFooter(1, 1, 1, b"\0" * 32, signer.certificate, b"\0" * 64)
    assert len(F.pack(f)) == F.SIZE
    for bad in (dict(image_sha256=b"\0" * 31), dict(certificate=b"\0" * 159), dict(signature=b"\0")):
        g = F.StubFooter(**{**f.__dict__, **bad})
        with pytest.raises(OtaError, match="stub footer"):
            F.pack(g)


def test_parse_rejects_damage(signer):
    sector = F.seal(b"code", 4096, stub_version=2, product_id=7,
                    certificate=signer.certificate, signer=signer)
    foot = bytearray(F.footer_of(sector))
    with pytest.raises(OtaError, match="too short"):
        F.footer_of(b"x" * 100)
    with pytest.raises(OtaError, match="need 512"):
        F.parse(foot[:100])
    bad = bytearray(foot)
    bad[0:4] = b"NOPE"
    with pytest.raises(OtaError, match="bad magic"):
        F.parse(bad)
    bad = bytearray(foot)
    struct.pack_into("<I", bad, 4, 9)
    with pytest.raises(OtaError, match="unsupported version 9"):
        F.parse(bytes(bad))
    bad = bytearray(foot)
    bad[40] ^= 1                              # inside the hash: crc catches it
    with pytest.raises(OtaError, match="crc32 mismatch"):
        F.parse(bytes(bad))
    bad = bytearray(foot)
    struct.pack_into("<Q", bad, 24, 1)       # reserved, with a fixed crc
    import binascii
    struct.pack_into("<I", bad, F._CRC_OFFSET, binascii.crc32(bytes(bad[:F._CRC_OFFSET])) & 0xFFFFFFFF)
    with pytest.raises(OtaError, match="reserved"):
        F.parse(bytes(bad))


def _recrc(foot: bytearray) -> bytes:
    import binascii
    struct.pack_into("<I", foot, F._CRC_OFFSET, binascii.crc32(bytes(foot[:F._CRC_OFFSET])) & 0xFFFFFFFF)
    return bytes(foot)


def test_verify_names_each_failure(signer):
    code = b"the stub" * 50
    sector = F.seal(code, 4096, stub_version=1, product_id=1,
                    certificate=signer.certificate, signer=signer)
    body, foot = sector[:-F.SIZE], bytearray(sector[-F.SIZE:])
    # corrupt footer
    ok, why = F.verify(body + b"\0" * F.SIZE, trust.ROOTS)
    assert not ok and why.startswith("invalid stub footer")
    # a certificate from a root the project does not have
    other_root = keys.generate_private_key(trust.SPEC)
    stranger = _Signer(keys.generate_private_key(trust.SPEC))
    stranger.certificate = trust.certificate(stranger.priv, kind=C.KIND_FACTORY, root_priv=other_root)
    s2 = F.seal(code, 4096, stub_version=1, product_id=1, certificate=stranger.certificate, signer=stranger)
    ok, why = F.verify(s2, trust.ROOTS)
    assert not ok and why.startswith("untrusted certificate")
    # a signature by a key other than the certified one
    impostor = _Signer(keys.generate_private_key(trust.SPEC))
    impostor.certificate = signer.certificate
    s3 = F.seal(code, 4096, stub_version=1, product_id=1, certificate=signer.certificate, signer=impostor)
    ok, why = F.verify(s3, trust.ROOTS)
    assert not ok and why.startswith("signature does not verify")
    # the code tampered with (the footer still verifies as bytes)
    ok, why = F.verify(b"X" + body[1:] + bytes(foot), trust.ROOTS)
    assert not ok and "code hash mismatch" in why
    # a footer whose image_size overruns the sector it sits in
    assert F.verify(body[:600] + bytes(foot), trust.ROOTS)[0]   # a shorter sector still holds the code
    f = F.parse(bytes(foot))
    f.image_size = 10_000
    f.signature = signer.sign(F.signed_region(f))
    ok, why = F.verify(body + F.pack(f), trust.ROOTS)
    assert not ok and why.startswith("footer names 10000 bytes of code")

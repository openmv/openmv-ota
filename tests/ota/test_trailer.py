"""Tests for the OTA trailer codec (pure, no crypto)."""

from __future__ import annotations

import binascii
import struct

import pytest

from openmv_ota.ota import (
    ES256,
    HEADER_SIZE,
    MAGIC_ROMFS_APP,
    Component,
    Trailer,
    pack_trailer,
    parse_trailer,
    signed_region,
)
from openmv_ota.ota import certificate as cert_mod
from openmv_ota.ota import trailer as trailer_mod
from openmv_ota.ota.errors import OtaError
from tests import trust

_CERT = trust.certificate(trust.ROOT_PRIV)            # any well-formed certificate will do
_COMP = Component(dest=0x70090000, offset=4096, size=1000, installed_size=2000,
                  sha256=bytes(range(32)))


def _trailer(**over) -> Trailer:
    fields = dict(
        body_size=1234,
        meta={"build": "x", "version": 3},
        product_id=0x1234,
        payload_version=7,
        body_sha256=bytes(range(32)),
        certificate=_CERT,
        signature=b"\x00" * 64,
    )
    fields.update(over)
    return Trailer(**fields)


def _raw_header(**over) -> bytes:
    """A raw 96-byte header with valid defaults; override any field."""
    f = dict(
        magic=MAGIC_ROMFS_APP, header_version=trailer_mod.HEADER_VERSION, body_size=0,
        comp_count=0, meta_size=0, min_stub=0, payload_version=0,
        uart_en=0, uart_id=0, wdt_en=0, wdt_id=0, baud=0, wdt_timeout_ms=0,
        product_id=0, publish_seq=0, reserved=0, body_sha256=b"\x00" * 32,
    )
    f.update(over)
    return struct.pack(
        trailer_mod.HEADER_STRUCT, f["magic"], f["header_version"], f["body_size"],
        f["comp_count"], f["meta_size"], f["min_stub"], f["payload_version"],
        f["uart_en"], f["uart_id"], f["wdt_en"], f["wdt_id"], f["baud"], f["wdt_timeout_ms"],
        f["product_id"], f["publish_seq"], f["reserved"], f["body_sha256"],
    )


def _assemble(meta_bytes=b"{}", sig_bytes=b"\x00" * 64, cert=_CERT, comps=b"", **over) -> bytes:
    """Raw trailer (header || comps || cert || meta || sig || crc) with a correct crc."""
    over.setdefault("meta_size", len(meta_bytes))
    over.setdefault("comp_count", len(comps) // trailer_mod.COMPONENT_SIZE)
    body = _raw_header(**over) + comps + cert + meta_bytes + sig_bytes
    return body + struct.pack("<I", binascii.crc32(body) & 0xFFFFFFFF)


# --- round-trip -------------------------------------------------------------

def test_round_trip():
    t = _trailer()
    parsed = parse_trailer(pack_trailer(t))
    assert parsed == t


def test_round_trip_with_components_and_stub_fields():
    t = _trailer(components=[_COMP, Component(0x70200000, 8192, 10, 20, b"\xaa" * 32)],
                 min_stub=3, uart_en=1, uart_id=4, baud=115200, wdt_en=1, wdt_id=1,
                 wdt_timeout_ms=8000, publish_seq=9)
    parsed = parse_trailer(pack_trailer(t))
    assert parsed == t
    assert parsed.components[0].dest == 0x70090000 and parsed.components[1].sha256 == b"\xaa" * 32


def test_derived_fields_come_from_the_certificate():
    t = parse_trailer(pack_trailer(_trailer()))
    assert t.sig_alg == ES256
    assert t.key_id == cert_mod.key_id_of(trust.ROOT_PUB)
    assert t.cert.pubkey == trust.ROOT_PUB


def test_layout_sizes_and_offsets():
    """The byte layout the boot stub is built against -- permanent once a stub ships."""
    assert HEADER_SIZE == 96 and HEADER_SIZE % 16 == 0
    assert trailer_mod.COMPONENT_SIZE == 48 and trailer_mod.CERT_SIZE == 160
    out = pack_trailer(_trailer(components=[_COMP], min_stub=5, uart_en=1, uart_id=2, wdt_en=1,
                                wdt_id=3, baud=921600, wdt_timeout_ms=4000, product_id=0x11,
                                publish_seq=0x22))
    assert struct.unpack_from("<I", out, 12)[0] == 1            # comp_count
    assert struct.unpack_from("<I", out, 20)[0] == 5            # min_stub
    assert struct.unpack_from("<I", out, 24)[0] == 7            # payload_version
    assert out[28:32] == bytes([1, 2, 1, 3])                    # uart_en uart_id wdt_en wdt_id
    assert struct.unpack_from("<II", out, 32) == (921600, 4000)  # baud, wdt_timeout_ms
    assert struct.unpack_from("<QQQ", out, 40) == (0x11, 0x22, 0)
    assert out[64:96] == bytes(range(32))                       # body_sha256
    assert struct.unpack_from(trailer_mod.COMPONENT_STRUCT, out, 96) == (
        0x70090000, 4096, 1000, 2000, bytes(range(32)))
    assert out[96 + 48:96 + 48 + 160] == _CERT


# --- signed region ----------------------------------------------------------

def test_signed_region_trailer_matches_packed():
    t = _trailer(components=[_COMP])
    out = pack_trailer(t)
    region = signed_region(t)
    assert region == signed_region(out)
    assert region == out[: len(out) - len(t.signature) - 4]
    assert region.startswith(MAGIC_ROMFS_APP)


def test_signed_region_before_signing():
    # The build flow signs signed_region(t) with signature still empty.
    t = _trailer(signature=b"")
    region = signed_region(t)
    assert len(region) == HEADER_SIZE + 160 + len(trailer_mod._serialize_meta(t.meta))


def test_meta_determinism():
    a = signed_region(_trailer(meta={"a": 1, "b": 2}))
    b = signed_region(_trailer(meta={"b": 2, "a": 1}))
    assert a == b


def test_meta_non_ascii_round_trips():
    t = _trailer(meta={"name": "café", "note": "✓"})
    assert parse_trailer(pack_trailer(t)).meta == t.meta


def test_signed_region_bytes_too_short():
    with pytest.raises(OtaError, match="too small"):
        signed_region(b"short")


def test_signed_region_bytes_overflow():
    with pytest.raises(OtaError, match="exceeds"):
        signed_region(_raw_header(meta_size=1000) + _CERT)  # claims 1000 bytes of meta


def test_signed_region_rejects_an_absurd_component_count():
    with pytest.raises(OtaError, match="over the 16-component limit"):
        signed_region(_raw_header(comp_count=1000))


# --- pack rejections --------------------------------------------------------

def test_pack_bad_signature_length():
    with pytest.raises(OtaError, match="signature must be 64 bytes"):
        pack_trailer(_trailer(signature=b"\x00" * 10))


def test_pack_bad_body_sha_length():
    with pytest.raises(OtaError, match="body_sha256 must be 32 bytes"):
        pack_trailer(_trailer(body_sha256=b"\x00" * 10))


def test_pack_bad_component_sha_length():
    with pytest.raises(OtaError, match="component sha256 must be 32 bytes"):
        pack_trailer(_trailer(components=[Component(1, 2, 3, 4, b"\x00" * 5)]))


def test_pack_too_many_components():
    with pytest.raises(OtaError, match="over the 16-component limit"):
        pack_trailer(_trailer(components=[_COMP] * 17))


def test_pack_bad_certificate():
    with pytest.raises(OtaError, match="certificate must be 160 bytes"):
        pack_trailer(_trailer(certificate=b"\x00" * 10))
    with pytest.raises(OtaError, match="bad certificate magic"):
        pack_trailer(_trailer(certificate=b"\x00" * 160))


def test_the_64_bit_fields_carry_their_full_width():
    """A 32-bit product id collides at a few thousand products (birthday bound), and a
    collision means one product line's devices accept another's firmware. ``publish_seq``
    is 64-bit because it is an account-wide counter with no reason to ever run out, and
    ``reserved`` is the headroom the NEXT field comes out of -- header_version is a
    one-shot (see the module docstring), so the space has to be there in advance."""
    assert trailer_mod.HEADER_VERSION == 4
    big = (1 << 64) - 1
    t = parse_trailer(pack_trailer(_trailer(product_id=big, publish_seq=big, reserved=big)))
    assert (t.product_id, t.publish_seq, t.reserved) == (big, big, big)


def test_publish_seq_defaults_to_zero():
    t = parse_trailer(pack_trailer(_trailer()))
    assert t.publish_seq == 0 and t.reserved == 0 and t.components == []


def test_pack_oversize():
    big = _trailer(meta={"blob": "A" * 4100})
    with pytest.raises(OtaError, match="over the 4096-byte limit"):
        pack_trailer(big)


# --- parse rejections -------------------------------------------------------

def test_parse_too_small():
    with pytest.raises(OtaError, match="too small"):
        parse_trailer(b"\x00" * 10)


def test_parse_bad_magic():
    with pytest.raises(OtaError, match="bad magic"):
        parse_trailer(_assemble(magic=b"XXXX"))


def test_parse_bad_header_version():
    """Exact equality, both directions -- and that is deliberate, not laziness.

    Every earlier version is a DIFFERENT layout (3 had no components or certificate), so a
    reader that accepted one would misread every field after the point they diverge. Refusing
    a LATER version matters for the same reason, and costs what the module docstring says it
    costs: the boot stub cannot be replaced over the air, so the version is a one-shot."""
    for bad in (1, 2, 3, 5):
        with pytest.raises(OtaError, match="unsupported header_version %d" % bad):
            parse_trailer(_assemble(header_version=bad))


def test_parse_bad_certificate():
    with pytest.raises(OtaError, match="bad certificate magic"):
        parse_trailer(_assemble(cert=b"\x00" * 160))


def test_parse_truncated_body():
    out = pack_trailer(_trailer())
    with pytest.raises(OtaError, match="truncated"):
        parse_trailer(out[:-1])


def test_parse_truncated_before_the_certificate():
    with pytest.raises(OtaError, match="exceeds"):
        parse_trailer(_raw_header(comp_count=2))


def test_parse_crc_mismatch():
    out = bytearray(pack_trailer(_trailer()))
    out[HEADER_SIZE + 160] ^= 0xFF  # corrupt a meta byte; stored crc no longer matches
    with pytest.raises(OtaError, match="crc32 mismatch"):
        parse_trailer(bytes(out))


def test_parse_bad_json_meta():
    with pytest.raises(OtaError, match="not valid JSON"):
        parse_trailer(_assemble(meta_bytes=b"not json"))


@pytest.mark.parametrize("meta", [b"[1, 2]", b"7", b'"s"', b"null"])
def test_parse_meta_that_is_not_an_object(meta):
    """Every reader does ``meta.get(...)``: a list or a number is a malformed trailer here,
    not an AttributeError in whatever reads it next."""
    with pytest.raises(OtaError, match="not a JSON object"):
        parse_trailer(_assemble(meta_bytes=meta))


def test_parse_meta_nested_past_the_recursion_limit():
    """parse_trailer takes a file of any size; JSON nested deep enough raised RecursionError,
    which no caller catches (verify_image only catches OtaError)."""
    deep = b"[" * 50_000 + b"]" * 50_000
    with pytest.raises(OtaError, match="not valid JSON"):
        parse_trailer(_assemble(meta_bytes=deep))

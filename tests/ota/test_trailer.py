"""Tests for the OTA trailer codec (pure, no crypto)."""

from __future__ import annotations

import binascii
import struct

import pytest

from openmv_ota.ota import (
    ES256,
    ES384,
    ES512,
    HEADER_SIZE,
    MAGIC_ROMFS_APP,
    Trailer,
    pack_trailer,
    parse_trailer,
    signed_region,
)
from openmv_ota.ota import trailer as trailer_mod
from openmv_ota.ota.errors import OtaError


def _trailer(**over) -> Trailer:
    fields = dict(
        body_size=1234,
        pad_size=8,
        meta={"build": "x", "version": 3},
        board_id=0x1234,
        min_platform_version=(5 << 24),
        payload_version=7,
        payload_version_floor=2,
        key_id=0x10,
        sig_alg=ES256,
        body_sha256=bytes(range(32)),
        signature=b"\x00" * 64,
    )
    fields.update(over)
    return Trailer(**fields)


def _raw_header(**over) -> bytes:
    """A raw 80-byte header with valid-ES256 defaults; override any field."""
    f = dict(
        magic=MAGIC_ROMFS_APP, header_version=1, body_size=0, pad_size=0, meta_size=0,
        sig_size=64, board_id=0, min_platform_version=0, payload_version=0,
        payload_version_floor=0, key_id=0, sig_alg=ES256, body_sha256=b"\x00" * 32,
    )
    f.update(over)
    return struct.pack(
        trailer_mod.HEADER_STRUCT, f["magic"], f["header_version"], f["body_size"],
        f["pad_size"], f["meta_size"], f["sig_size"], f["board_id"],
        f["min_platform_version"], f["payload_version"], f["payload_version_floor"],
        f["key_id"], f["sig_alg"], f["body_sha256"],
    )


def _assemble(meta_bytes=b"{}", sig_bytes=b"\x00" * 64, **over) -> bytes:
    """Raw trailer (header || meta || sig || crc) with a correct crc, for parse tests."""
    over.setdefault("meta_size", len(meta_bytes))
    over.setdefault("sig_size", len(sig_bytes))
    body = _raw_header(**over) + meta_bytes + sig_bytes
    return body + struct.pack("<I", binascii.crc32(body) & 0xFFFFFFFF)


# --- round-trip -------------------------------------------------------------

def test_round_trip():
    t = _trailer()
    parsed = parse_trailer(pack_trailer(t))
    assert parsed == t


@pytest.mark.parametrize("alg, siglen", [(ES256, 64), (ES384, 96), (ES512, 132)])
def test_round_trip_each_curve(alg, siglen):
    t = _trailer(sig_alg=alg, signature=b"\xab" * siglen)
    out = pack_trailer(t)
    parsed = parse_trailer(out)
    assert parsed.sig_alg == alg
    assert len(parsed.signature) == siglen
    assert parsed == t


def test_header_is_80_bytes():
    assert HEADER_SIZE == 80
    assert len(_raw_header()) == 80


# --- signed region ----------------------------------------------------------

def test_signed_region_trailer_matches_packed():
    t = _trailer()
    out = pack_trailer(t)
    region = signed_region(t)
    assert region == signed_region(out)
    meta_size = len(out) - HEADER_SIZE - len(t.signature) - 4
    assert region == out[: HEADER_SIZE + meta_size]
    assert region.startswith(MAGIC_ROMFS_APP)


def test_signed_region_before_signing():
    # The build flow signs signed_region(t) with signature still empty.
    t = _trailer(signature=b"")
    region = signed_region(t)
    assert len(region) == HEADER_SIZE + len(trailer_mod._serialize_meta(t.meta))


def test_meta_determinism():
    # Insertion order must not change the bytes (sorted keys).
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
        signed_region(_raw_header(meta_size=1000))  # 80 bytes claim 1000 of meta


# --- pack rejections --------------------------------------------------------

def test_pack_bad_signature_length():
    with pytest.raises(OtaError, match="signature must be 64 bytes"):
        pack_trailer(_trailer(signature=b"\x00" * 10))


def test_pack_bad_body_sha_length():
    with pytest.raises(OtaError, match="body_sha256 must be 32 bytes"):
        pack_trailer(_trailer(body_sha256=b"\x00" * 10))


def test_pack_unsupported_algorithm():
    with pytest.raises(OtaError, match="unknown COSE algorithm id 999"):
        pack_trailer(_trailer(sig_alg=999, signature=b"\x00" * 64))


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
    with pytest.raises(OtaError, match="unsupported header_version 2"):
        parse_trailer(_assemble(header_version=2))


def test_parse_unknown_algorithm():
    with pytest.raises(OtaError, match="unknown COSE algorithm id 999"):
        parse_trailer(_assemble(sig_alg=999))


def test_parse_sig_size_mismatch():
    # Valid algorithm, but the stored sig_size disagrees with it.
    raw = _assemble(sig_bytes=b"\x00" * 10, sig_alg=ES256)  # sig_size=10, ES256 wants 64
    with pytest.raises(OtaError, match="sig_size 10 != 64"):
        parse_trailer(raw)


def test_parse_truncated_body():
    out = pack_trailer(_trailer())
    with pytest.raises(OtaError, match="truncated"):
        parse_trailer(out[:-1])


def test_parse_crc_mismatch():
    out = bytearray(pack_trailer(_trailer()))
    out[HEADER_SIZE] ^= 0xFF  # corrupt a meta byte; stored crc no longer matches
    with pytest.raises(OtaError, match="crc32 mismatch"):
        parse_trailer(bytes(out))


def test_parse_bad_json_meta():
    raw = _assemble(meta_bytes=b"not json", sig_bytes=b"\x00" * 64, sig_alg=ES256)
    with pytest.raises(OtaError, match="not valid JSON"):
        parse_trailer(raw)

"""Tests for the signed OTA update-manifest codec + policy (openmv_ota.ota.manifest)
and its host-side verification (openmv_ota.ota.verify.verify_manifest)."""

from __future__ import annotations

import binascii
import struct

import pytest

from openmv_ota.ota import ES256, ES384, algorithm_for
from openmv_ota.ota.errors import OtaError
from openmv_ota.ota.keys import TrustedKey, generate_private_key, public_point_hex
from openmv_ota.ota.manifest import (
    HEADER_STRUCT,
    HEADER_SIZE,
    MAGIC,
    Manifest,
    pack_manifest,
    parse_manifest,
    select_representation,
    signed_region,
    update_reject_reason,
)
from openmv_ota.ota.sign import sign_region
from openmv_ota.ota.verify import verify_manifest
from openmv_ota.ota.version import encode_app_version


def _body(**over):
    body = {
        "schema": 1,
        "product_id": 7,
        "product": "OPENMV_N6",
        "version": "2.1.0",
        "payload_version": encode_app_version("2.1.0"),
        "min_platform_version": 0,
        "size": 4 * 4096,
        "sha256": "ab" * 32,
        "representations": [
            {"format": "full", "url": "https://dl.x.io/n6-v2.img.gz", "size": 900000},
        ],
    }
    body.update(over)
    return body


def _make(body=None, alg=ES256, key_id=0x0100):
    """A freshly signed manifest -> (manifest_bytes, pubkey_hex)."""
    spec = algorithm_for(alg)
    priv = generate_private_key(spec)
    m = Manifest(body=_body() if body is None else body, key_id=key_id, sig_alg=alg)
    m.signature = sign_region(priv, signed_region(m), spec)
    return pack_manifest(m), public_point_hex(priv.public_key())


def _trusted(key_id=0x0100, alg=ES256, pubkey="", revoked=False):
    return [TrustedKey(key_id, alg, "ota", pubkey, revoked=revoked)]


# --- codec roundtrip --------------------------------------------------------

@pytest.mark.parametrize("alg", [ES256, ES384])
def test_roundtrip(alg):
    raw, _pub = _make(alg=alg, key_id=0x0123)
    m = parse_manifest(raw)
    assert m.body == _body()
    assert m.key_id == 0x0123
    assert m.sig_alg == alg
    assert len(m.signature) == algorithm_for(alg).sig_size


def test_signed_region_matches_between_object_and_bytes():
    raw, _pub = _make()
    m = parse_manifest(raw)
    assert signed_region(m) == signed_region(raw)            # same bytes both ways
    assert signed_region(raw) == raw[: HEADER_SIZE + len(signed_region(m)) - HEADER_SIZE]


# --- codec rejections -------------------------------------------------------

def test_parse_too_small():
    with pytest.raises(OtaError, match="too small"):
        parse_manifest(b"\x00" * 4)


def test_parse_bad_magic():
    raw = bytearray(_make()[0])
    raw[0:4] = b"XXXX"
    with pytest.raises(OtaError, match="bad magic"):
        parse_manifest(bytes(raw))


def test_parse_unsupported_header_version():
    m = Manifest(body=_body(), key_id=0x0100, sig_alg=ES256,
                 signature=b"\x00" * 64, header_version=2)
    with pytest.raises(OtaError, match="header_version"):
        parse_manifest(pack_manifest(m))


def test_parse_sig_size_disagrees_with_alg():
    # valid ES256 framing, but rewrite sig_alg to ES384 (sig_size 64 != 96) -> rejected
    # before the crc check (alg/size is validated first).
    raw = bytearray(_make()[0])
    struct.pack_into("<i", raw, struct.calcsize("<4sIIII"), ES384)
    with pytest.raises(OtaError, match="sig_size"):
        parse_manifest(bytes(raw))


def test_parse_unknown_algorithm():
    raw = bytearray(_make()[0])
    struct.pack_into("<i", raw, struct.calcsize("<4sIIII"), -99)
    with pytest.raises(OtaError, match="COSE"):
        parse_manifest(bytes(raw))


def test_parse_truncated():
    raw = _make()[0]
    with pytest.raises(OtaError, match="truncated"):
        parse_manifest(raw[:-1])


def test_parse_bad_crc():
    raw = bytearray(_make()[0])
    raw[-1] ^= 0xFF
    with pytest.raises(OtaError, match="crc32"):
        parse_manifest(bytes(raw))


def test_parse_non_json_body():
    # Hand-frame a structurally valid ES256 manifest whose body bytes aren't JSON.
    body = b"not json"
    sig = b"\x00" * 64
    header = struct.pack(HEADER_STRUCT, MAGIC, 1, len(body), 64, 0x0100, ES256)
    framed = header + body + sig
    raw = framed + struct.pack("<I", binascii.crc32(framed) & 0xFFFFFFFF)
    with pytest.raises(OtaError, match="not valid JSON"):
        parse_manifest(raw)


def test_signed_region_bytes_out_of_bounds():
    header = struct.pack(HEADER_STRUCT, MAGIC, 1, 9999, 64, 0x0100, ES256)
    with pytest.raises(OtaError, match="exceeds"):
        signed_region(header)


def test_signed_region_bytes_too_small():
    with pytest.raises(OtaError, match="too small"):
        signed_region(b"\x00" * 4)


def test_pack_wrong_signature_size():
    m = Manifest(body=_body(), key_id=0x0100, sig_alg=ES256, signature=b"\x00" * 10)
    with pytest.raises(OtaError, match="signature must be 64"):
        pack_manifest(m)


def test_pack_over_size_limit():
    big = _body(blob="z" * 9000)                            # body alone exceeds the cap
    m = Manifest(body=big, key_id=0x0100, sig_alg=ES256, signature=b"\x00" * 64)
    with pytest.raises(OtaError, match="over the"):
        pack_manifest(m)


# --- verify_manifest --------------------------------------------------------

def test_verify_ok():
    raw, pub = _make()
    ok, reason = verify_manifest(raw, _trusted(pubkey=pub))
    assert ok and "ES256" in reason


def test_verify_invalid_manifest():
    ok, reason = verify_manifest(b"\x00" * 8, _trusted())
    assert not ok and "invalid manifest" in reason


def test_verify_unknown_key():
    raw, pub = _make(key_id=0x0100)
    ok, reason = verify_manifest(raw, _trusted(key_id=0x0999, pubkey=pub))
    assert not ok and "unknown key" in reason


def test_verify_revoked_key():
    raw, pub = _make()
    ok, reason = verify_manifest(raw, _trusted(pubkey=pub, revoked=True))
    assert not ok and "revoked" in reason


def test_verify_algorithm_mismatch():
    raw, pub = _make(alg=ES256)
    ok, reason = verify_manifest(raw, _trusted(pubkey=pub, alg=ES384))
    assert not ok and "algorithm mismatch" in reason


def test_verify_bad_signature():
    raw, _pub = _make()
    other = public_point_hex(generate_private_key(algorithm_for(ES256)).public_key())
    ok, reason = verify_manifest(raw, _trusted(pubkey=other))
    assert not ok and "does not verify" in reason


# --- update_reject_reason (device-relative pre-flight) ----------------------

def test_reject_schema():
    assert update_reject_reason(_body(schema=2), product_id=7, platform_version=0,
                                rollback_floor=0) == "schema"


def test_reject_board_mismatch():
    assert update_reject_reason(_body(product_id=9), product_id=7, platform_version=0,
                                rollback_floor=0) == "board"


def test_reject_compat_floor():
    assert update_reject_reason(_body(min_platform_version=(6 << 24)), product_id=7,
                                platform_version=(5 << 24), rollback_floor=0) == "compat"


def test_reject_rollback():
    floor = encode_app_version("3.0.0")
    assert update_reject_reason(_body(), product_id=7, platform_version=0,
                                rollback_floor=floor) == "rollback"


def test_accept_when_all_clear():
    assert update_reject_reason(_body(), product_id=7, platform_version=(9 << 24),
                                rollback_floor=0) is None


def test_accept_product_id_zero_disables_check():
    # device product_id 0 == "don't check": a manifest for another board still passes
    assert update_reject_reason(_body(product_id=9), product_id=0, platform_version=0,
                                rollback_floor=0) is None


# --- select_representation --------------------------------------------------

_FULL = {"format": "full", "url": "https://x/full.gz", "size": 900000}
_DELTA = {"format": "ocdl", "url": "https://x/d.gz", "size": 40000,
          "base_payload_version": encode_app_version("1.0.0")}


def test_select_full_when_only_full():
    body = {"representations": [_FULL]}
    assert select_representation(body, delta_capable=True, golden_payload_version=0) is _FULL


def test_select_prefers_smaller_delta_when_capable_and_base_matches():
    body = {"representations": [_FULL, _DELTA]}
    got = select_representation(body, delta_capable=True,
                               golden_payload_version=encode_app_version("1.0.0"))
    assert got is _DELTA


def test_select_falls_back_to_full_when_not_delta_capable():
    body = {"representations": [_FULL, _DELTA]}
    got = select_representation(body, delta_capable=False,
                               golden_payload_version=encode_app_version("1.0.0"))
    assert got is _FULL


def test_select_skips_delta_on_base_mismatch():
    body = {"representations": [_FULL, _DELTA]}
    got = select_representation(body, delta_capable=True,
                               golden_payload_version=encode_app_version("2.0.0"))
    assert got is _FULL


def test_select_skips_unknown_format_and_repr_without_size():
    weird = {"format": "lzma", "url": "https://x/w.gz", "size": 1}
    nosize = {"format": "full", "url": "https://x/n.gz"}     # no size -> treated as huge
    body = {"representations": [weird, nosize, _FULL]}
    assert select_representation(body, delta_capable=True, golden_payload_version=0) is _FULL


def test_select_none_when_nothing_usable():
    body = {"representations": [_DELTA]}                     # delta only, not capable
    assert select_representation(body, delta_capable=False, golden_payload_version=0) is None

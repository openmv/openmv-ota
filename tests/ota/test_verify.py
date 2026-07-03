"""Tests for host-side OTA image verification (verify_image)."""

from __future__ import annotations

import hashlib

from openmv_ota.ota import ES256, ES384, Trailer, algorithm_for, pack_trailer, signed_region
from openmv_ota.ota.keys import TrustedKey, generate_private_key, public_point_hex
from openmv_ota.ota.sign import sign_region
from openmv_ota.ota.verify import verify_image
from openmv_ota.ota.version import encode_app_version


def _signed(alg=ES256, key_id=0x0100):
    """Return (body, trailer_bytes, pubkey_hex) for a freshly signed image."""
    spec = algorithm_for(alg)
    priv = generate_private_key(spec)
    body = b"ROMFS-BODY" * 40
    t = Trailer(
        body_size=len(body), pad_size=16, meta={"product": "p", "app_version": "1.2.3"},
        product_id=7, min_platform_version=(5 << 24), payload_version=encode_app_version("1.2.3"),
        payload_version_floor=0, key_id=key_id, sig_alg=alg,
        body_sha256=hashlib.sha256(body).digest())
    t.signature = sign_region(priv, signed_region(t), spec)
    return body, pack_trailer(t), public_point_hex(priv.public_key())


def _trusted(key_id=0x0100, alg=ES256, pubkey="", revoked=False):
    return [TrustedKey(key_id, alg, "ota", pubkey, revoked=revoked)]


def test_verify_ok():
    body, trailer, pub = _signed()
    ok, reason = verify_image(body, trailer, _trusted(pubkey=pub))
    assert ok and "body sha256 OK" in reason


def test_verify_unknown_key():
    body, trailer, pub = _signed(key_id=0x0100)
    ok, reason = verify_image(body, trailer, _trusted(key_id=0x0999, pubkey=pub))
    assert not ok and "unknown key" in reason


def test_verify_revoked_key():
    body, trailer, pub = _signed()
    ok, reason = verify_image(body, trailer, _trusted(pubkey=pub, revoked=True))
    assert not ok and "revoked" in reason


def test_verify_algorithm_mismatch():
    body, trailer, pub = _signed(alg=ES256)
    ok, reason = verify_image(body, trailer, _trusted(pubkey=pub, alg=ES384))
    assert not ok and "algorithm mismatch" in reason


def test_verify_bad_signature():
    body, trailer, _pub = _signed()
    other = public_point_hex(generate_private_key(algorithm_for(ES256)).public_key())
    ok, reason = verify_image(body, trailer, _trusted(pubkey=other))  # wrong pubkey
    assert not ok and "does not verify" in reason


def test_verify_body_size_mismatch():
    body, trailer, pub = _signed()
    ok, reason = verify_image(body + b"x", trailer, _trusted(pubkey=pub))
    assert not ok and "body is" in reason


def test_verify_body_hash_mismatch():
    body, trailer, pub = _signed()
    tampered = bytearray(body)
    tampered[0] ^= 0xFF  # same length, different content
    ok, reason = verify_image(bytes(tampered), trailer, _trusted(pubkey=pub))
    assert not ok and "hash mismatch" in reason


def test_verify_invalid_trailer():
    body, _trailer, pub = _signed()
    ok, reason = verify_image(body, b"not-a-trailer", _trusted(pubkey=pub))
    assert not ok and "invalid trailer" in reason

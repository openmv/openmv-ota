"""Stateless capability tokens: round-trip, expiry, tamper/secret/format rejection."""

from __future__ import annotations

from openmv_ota.server import capability as cap


def test_mint_verify_roundtrip():
    t = cap.mint("secret", "rel_1", ttl=100, now=lambda: 1000)
    assert cap.verify("secret", t, now=lambda: 1000) == "rel_1"
    assert cap.verify("secret", t, now=lambda: 1099) == "rel_1"     # within ttl


def test_expired_token_rejected():
    t = cap.mint("secret", "rel_1", ttl=100, now=lambda: 1000)      # exp = 1100
    assert cap.verify("secret", t, now=lambda: 1101) is None


def test_tampered_signature_rejected():
    t = cap.mint("secret", "rel_1")
    body, sig = t.split(".", 1)
    assert cap.verify("secret", body + "." + "A" * len(sig)) is None


def test_wrong_secret_rejected():
    assert cap.verify("other-secret", cap.mint("secret", "rel_1")) is None


def test_malformed_tokens_rejected():
    assert cap.verify("secret", "no-dot-here") is None
    # a correctly-signed but non-JSON body must still be rejected
    body = cap._b64e(b"\xff\xff not json")
    tok = (body + b"." + cap._sig("secret", body)).decode()
    assert cap.verify("secret", tok) is None

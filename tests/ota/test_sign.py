"""Tests for host-side ECDSA signing/verification (real crypto, no device)."""

from __future__ import annotations

import pytest

from openmv_ota.ota import ES256, ES384, ES512, algorithm_for
from openmv_ota.ota.keys import generate_private_key
from openmv_ota.ota.sign import sign_region, verify_region

REGION = b"OMVR-signed-region-bytes" * 8


@pytest.mark.parametrize("cose_id", [ES256, ES384, ES512])
def test_sign_then_verify_round_trips(cose_id):
    alg = algorithm_for(cose_id)
    priv = generate_private_key(alg)
    sig = sign_region(priv, REGION, alg)
    assert len(sig) == alg.sig_size
    assert verify_region(priv.public_key(), REGION, sig, alg) is True


def test_verify_rejects_tampered_region():
    alg = algorithm_for(ES256)
    priv = generate_private_key(alg)
    sig = sign_region(priv, REGION, alg)
    assert verify_region(priv.public_key(), REGION + b"x", sig, alg) is False


def test_verify_rejects_corrupt_signature():
    alg = algorithm_for(ES256)
    priv = generate_private_key(alg)
    sig = bytearray(sign_region(priv, REGION, alg))
    sig[0] ^= 0xFF
    assert verify_region(priv.public_key(), REGION, bytes(sig), alg) is False


def test_verify_rejects_wrong_length_signature():
    alg = algorithm_for(ES256)
    priv = generate_private_key(alg)
    assert verify_region(priv.public_key(), REGION, b"\x00" * 10, alg) is False


def test_verify_rejects_other_key():
    alg = algorithm_for(ES256)
    sig = sign_region(generate_private_key(alg), REGION, alg)
    other = generate_private_key(alg)
    assert verify_region(other.public_key(), REGION, sig, alg) is False

"""Tests for ECDSA key generation, serialization, and trusted_keys.json."""

from __future__ import annotations

import pytest

from openmv_ota.ota import ES256, ES384, ES512, algorithm_for
from openmv_ota.ota.errors import OtaError
from openmv_ota.ota.keys import (
    TrustedKey,
    generate_private_key,
    load_private_key_pem,
    private_key_pem,
    public_key_from_hex,
    public_point_hex,
    read_trusted_keys,
    write_trusted_keys,
)
from openmv_ota.ota.sign import sign_region, verify_region

REGION = b"region-to-sign" * 4


@pytest.mark.parametrize("cose_id, curve", [
    (ES256, "secp256r1"), (ES384, "secp384r1"), (ES512, "secp521r1"),
])
def test_generate_uses_the_right_curve(cose_id, curve):
    priv = generate_private_key(algorithm_for(cose_id))
    assert priv.curve.name == curve


def test_public_point_hex_round_trips():
    alg = algorithm_for(ES256)
    priv = generate_private_key(alg)
    point_hex = public_point_hex(priv.public_key())
    assert point_hex.startswith("04")  # uncompressed point marker
    rebuilt = public_key_from_hex(point_hex, alg)
    # A signature verifies against the reconstructed public key.
    sig = sign_region(priv, REGION, alg)
    assert verify_region(rebuilt, REGION, sig, alg) is True


def test_public_key_from_hex_rejects_garbage():
    with pytest.raises(OtaError, match="invalid public point"):
        public_key_from_hex("00", algorithm_for(ES256))


def test_private_key_pem_round_trips():
    alg = algorithm_for(ES384)
    priv = generate_private_key(alg)
    pem = private_key_pem(priv)
    assert pem.startswith(b"-----BEGIN PRIVATE KEY-----")
    loaded = load_private_key_pem(pem)
    sig = sign_region(loaded, REGION, alg)
    assert verify_region(priv.public_key(), REGION, sig, alg) is True


def test_load_private_key_pem_rejects_garbage():
    with pytest.raises(OtaError, match="could not load private key"):
        load_private_key_pem(b"not a pem")


def test_trusted_key_dict_round_trip():
    k = TrustedKey(key_id=0x10, alg=ES256, role="ota", pubkey="04abcd")
    assert TrustedKey.from_dict(k.to_dict()) == k


def test_trusted_keys_file_round_trip(tmp_path):
    keys = [
        TrustedKey(0x01, ES256, "factory", "04aa"),
        TrustedKey(0x10, ES256, "ota", "04bb"),
    ]
    path = tmp_path / "trusted_keys.json"
    write_trusted_keys(path, keys)
    assert read_trusted_keys(path) == keys


def test_read_trusted_keys_missing(tmp_path):
    with pytest.raises(OtaError, match="no trusted_keys.json"):
        read_trusted_keys(tmp_path / "trusted_keys.json")


def test_read_trusted_keys_bad_json(tmp_path):
    path = tmp_path / "trusted_keys.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(OtaError, match="not valid JSON"):
        read_trusted_keys(path)

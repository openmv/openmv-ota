"""Tests for ECDSA key generation, serialization, and trusted_keys.json."""

from __future__ import annotations

import pytest

from openmv_ota.ota import ES256, ES384, ES512, algorithm_for
from openmv_ota.ota.errors import OtaError
from openmv_ota.ota.keys import (
    FACTORY_KEY_ID_BASE,
    OTA_KEY_ID_BASE,
    TrustedKey,
    generate_private_key,
    load_private_key_pem,
    private_key_pem,
    provision_key_set,
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


def test_provision_key_set():
    alg = algorithm_for(ES256)
    prov = provision_key_set(alg, n_factory=2, n_ota=3)

    # 2 factory + 3 ota, with separated, sequential key_ids and correct roles.
    assert [k.role for k in prov.trusted] == ["factory", "factory", "ota", "ota", "ota"]
    assert [k.key_id for k in prov.trusted] == [
        FACTORY_KEY_ID_BASE, FACTORY_KEY_ID_BASE + 1,
        OTA_KEY_ID_BASE, OTA_KEY_ID_BASE + 1, OTA_KEY_ID_BASE + 2,
    ]
    assert all(k.alg == ES256 for k in prov.trusted)
    assert prov.signing_key_id == OTA_KEY_ID_BASE
    assert set(prov.private_pems) == {k.key_id for k in prov.trusted}


def test_provisioned_private_keys_match_public_set():
    alg = algorithm_for(ES256)
    prov = provision_key_set(alg, n_factory=1, n_ota=1)
    by_id = {k.key_id: k for k in prov.trusted}
    region = b"provisioned-region"
    for key_id, pem in prov.private_pems.items():
        priv = load_private_key_pem(pem)
        pub = public_key_from_hex(by_id[key_id].pubkey, alg)
        assert verify_region(pub, region, sign_region(priv, region, alg), alg) is True

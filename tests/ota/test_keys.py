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
    pem = private_key_pem(priv, "s3cret")
    assert pem.startswith(b"-----BEGIN ENCRYPTED PRIVATE KEY-----")   # never plaintext
    loaded = load_private_key_pem(pem, "s3cret")
    sig = sign_region(loaded, REGION, alg)
    assert verify_region(priv.public_key(), REGION, sig, alg) is True


def test_private_key_pem_requires_passphrase():
    with pytest.raises(OtaError, match="passphrase is required"):
        private_key_pem(generate_private_key(algorithm_for(ES256)), "")


def test_load_encrypted_wrong_passphrase():
    pem = private_key_pem(generate_private_key(algorithm_for(ES256)), "right")
    with pytest.raises(OtaError, match="could not load private key"):
        load_private_key_pem(pem, "wrong")


def test_load_private_key_pem_rejects_garbage():
    with pytest.raises(OtaError, match="could not load private key"):
        load_private_key_pem(b"not a pem")


def test_spki_to_point_hex_round_trips():
    from cryptography.hazmat.primitives import serialization

    from openmv_ota.ota.keys import spki_to_point_hex
    pub = generate_private_key(algorithm_for(ES256)).public_key()
    der = pub.public_bytes(serialization.Encoding.DER,
                           serialization.PublicFormat.SubjectPublicKeyInfo)
    assert spki_to_point_hex(der) == public_point_hex(pub)   # KMS GetPublicKey output -> point


def test_spki_to_point_hex_rejects_garbage():
    from openmv_ota.ota.keys import spki_to_point_hex
    with pytest.raises(OtaError, match="could not parse public key DER"):
        spki_to_point_hex(b"not der")


def test_trusted_key_dict_round_trip():
    k = TrustedKey(key_id=0x10, alg=ES256, role="ota", pubkey="04abcd")
    assert TrustedKey.from_dict(k.to_dict()) == k
    # revoked round-trips, and an old entry without the field defaults to not-revoked.
    r = TrustedKey(key_id=0x11, alg=ES256, role="ota", pubkey="04ef", revoked=True)
    assert TrustedKey.from_dict(r.to_dict()) == r and r.to_dict()["revoked"] is True
    legacy = {"key_id": 1, "alg": ES256, "role": "ota", "pubkey": "04"}
    assert TrustedKey.from_dict(legacy).revoked is False


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
    prov = provision_key_set(alg, n_factory=2, n_ota=3, passphrase="pw")

    # 2 factory + 3 ota, with separated, sequential key_ids and correct roles.
    assert [k.role for k in prov.trusted] == ["factory", "factory", "ota", "ota", "ota"]
    assert [k.key_id for k in prov.trusted] == [
        FACTORY_KEY_ID_BASE, FACTORY_KEY_ID_BASE + 1,
        OTA_KEY_ID_BASE, OTA_KEY_ID_BASE + 1, OTA_KEY_ID_BASE + 2,
    ]
    assert all(k.alg == ES256 for k in prov.trusted)
    assert prov.signing_key_id == OTA_KEY_ID_BASE
    assert set(prov.private_pems) == {k.key_id for k in prov.trusted}
    # two roots, and every key certified by root 0 with its role as the certificate's kind
    from openmv_ota.ota import certificate as cert_mod
    from openmv_ota.ota.keys import root_keys_map
    assert [r.root_id for r in prov.roots] == [0, 1] and set(prov.root_pems) == {0, 1}
    for k in prov.trusted:
        c = cert_mod.verify(bytes.fromhex(k.cert), root_keys_map(prov.roots))
        assert c.root_id == 0 and c.epoch == 0 and c.pubkey.hex() == k.pubkey
        assert c.kind == (cert_mod.KIND_FACTORY if k.role == "factory" else cert_mod.KIND_OTA)


def test_provision_key_set_is_p256_only():
    from openmv_ota.ota import ES384
    with pytest.raises(OtaError, match="P-256 keys only"):
        provision_key_set(algorithm_for(ES384), n_factory=1, n_ota=1, passphrase="pw")


def test_trusted_key_cert_round_trips_and_defaults_empty():
    k = TrustedKey(key_id=0x10, alg=ES256, role="ota", pubkey="04abcd", cert="aa" * 160)
    assert TrustedKey.from_dict(k.to_dict()) == k
    assert TrustedKey.from_dict({"key_id": 1, "alg": ES256, "role": "ota", "pubkey": "04"}).cert == ""


def test_root_keys_file_round_trip(tmp_path):
    from openmv_ota.ota.keys import RootKey, read_root_keys, root_keys_map, write_root_keys
    path = tmp_path / "root_keys.json"
    roots = [RootKey(0, ES256, "04" + "aa" * 64), RootKey(1, ES256, "04" + "bb" * 64)]
    write_root_keys(path, roots)
    assert read_root_keys(path) == roots
    assert root_keys_map(roots) == {0: bytes.fromhex(roots[0].pubkey), 1: bytes.fromhex(roots[1].pubkey)}
    with pytest.raises(OtaError, match="no root_keys.json"):
        read_root_keys(tmp_path / "absent.json")
    path.write_text("{not json")
    with pytest.raises(OtaError, match="not valid JSON"):
        read_root_keys(path)


def test_certify_refuses_an_unknown_role():
    from openmv_ota.ota.keys import certify, generate_private_key
    root = generate_private_key(algorithm_for(ES256))
    with pytest.raises(OtaError, match="no certificate kind for a 'desk' key"):
        certify(root, 0, [TrustedKey(1, ES256, "desk", "04" + "00" * 64)])


def test_provisioned_private_keys_match_public_set():
    alg = algorithm_for(ES256)
    prov = provision_key_set(alg, n_factory=1, n_ota=1, passphrase="pw")
    by_id = {k.key_id: k for k in prov.trusted}
    region = b"provisioned-region"
    for key_id, pem in prov.private_pems.items():
        priv = load_private_key_pem(pem, "pw")
        pub = public_key_from_hex(by_id[key_id].pubkey, alg)
        assert verify_region(pub, region, sign_region(priv, region, alg), alg) is True

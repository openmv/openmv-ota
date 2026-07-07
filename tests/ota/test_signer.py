"""The pluggable Signer: build_signer dispatch, LocalSigner, the custom hook."""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization

from openmv_ota.ota import algorithm_for
from openmv_ota.ota.algorithms import ES256
from openmv_ota.ota.errors import OtaError
from openmv_ota.ota.keys import TrustedKey, generate_private_key, private_key_pem, public_point_hex
from openmv_ota.ota.sign import verify_region
from openmv_ota.ota.signer import LocalSigner, Signer, build_signer

ALG = algorithm_for(ES256)


class _FakeSigner(Signer):
    key_id = 1
    sig_alg = ES256
    alg = ALG

    def sign(self, region):
        return b"\x00" * ALG.sig_size

    def public_point_hex(self):
        return "aa"


def _entry_and_key(tmp_path, role="ota", key_id=0x0100, enc=None):
    key = generate_private_key(ALG)
    pem = (private_key_pem(key) if enc is None else key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(enc.encode())))
    (tmp_path / ("%s-%04x.pem" % (role, key_id))).write_bytes(pem)
    entry = TrustedKey(key_id=key_id, alg=ES256, role=role, pubkey=public_point_hex(key.public_key()))
    return entry, key


def test_local_signer_signs_and_exposes_point(tmp_path):
    entry, key = _entry_and_key(tmp_path)
    s = build_signer(entry, ALG, private_keys_dir=tmp_path, backend={})
    assert isinstance(s, LocalSigner) and not s.is_dev_key
    region = b"a signed region"
    sig = s.sign(region)
    assert len(sig) == ALG.sig_size and verify_region(key.public_key(), region, sig, ALG)
    assert s.public_point_hex() == entry.pubkey


def test_build_signer_unknown_backend(tmp_path):
    entry, _ = _entry_and_key(tmp_path)
    with pytest.raises(OtaError, match="unknown signer backend"):
        build_signer(entry, ALG, private_keys_dir=tmp_path, backend={"backend": "nope"})


def test_local_signer_missing_pem(tmp_path):
    entry, _ = _entry_and_key(tmp_path)
    (tmp_path / "ota-0100.pem").unlink()
    with pytest.raises(OtaError, match="not found"):
        build_signer(entry, ALG, private_keys_dir=tmp_path)


def test_passphrase_provider_marks_dev(tmp_path):
    entry, _ = _entry_and_key(tmp_path, enc="secret")     # an encrypted PEM
    s = build_signer(entry, ALG, private_keys_dir=tmp_path,
                     passphrase_provider=lambda: ("secret", "dev"))
    assert s.is_dev_key and s.public_point_hex() == entry.pubkey


def test_custom_signer_hook(tmp_path, monkeypatch):
    entry, _ = _entry_and_key(tmp_path)

    class _Mod:
        make = staticmethod(lambda e, a, b: _FakeSigner())
        not_a_signer = staticmethod(lambda e, a, b: object())

    monkeypatch.setattr("openmv_ota.ota.signer.importlib.import_module", lambda name: _Mod)
    s = build_signer(entry, ALG, private_keys_dir=tmp_path,
                     backend={"backend": "custom", "factory": "any.mod:make"})
    assert s.sign(b"x") == b"\x00" * ALG.sig_size and s.public_point_hex() == "aa"
    with pytest.raises(OtaError, match="did not return a Signer"):
        build_signer(entry, ALG, private_keys_dir=tmp_path,
                     backend={"backend": "custom", "factory": "any.mod:not_a_signer"})


def test_custom_signer_bad_ref(tmp_path):
    entry, _ = _entry_and_key(tmp_path)
    with pytest.raises(OtaError, match="needs a 'factory'"):
        build_signer(entry, ALG, private_keys_dir=tmp_path, backend={"backend": "custom"})
    with pytest.raises(OtaError, match="not importable"):
        build_signer(entry, ALG, private_keys_dir=tmp_path,
                     backend={"backend": "custom", "factory": "no_such_pkg.mod:x"})

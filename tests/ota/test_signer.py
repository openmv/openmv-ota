"""The pluggable Signer: build_signer dispatch, LocalSigner, the custom hook."""

from __future__ import annotations

import pytest

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


def _entry_and_key(tmp_path, role="ota", key_id=0x0100, passphrase="pw"):
    key = generate_private_key(ALG)
    (tmp_path / ("%s-%04x.pem" % (role, key_id))).write_bytes(private_key_pem(key, passphrase))
    entry = TrustedKey(key_id=key_id, alg=ES256, role=role, pubkey=public_point_hex(key.public_key()))
    return entry, key


def test_local_signer_signs_and_exposes_point(tmp_path):
    entry, key = _entry_and_key(tmp_path)
    s = build_signer(entry, ALG, private_keys_dir=tmp_path, backend={},
                     passphrase_provider=lambda: ("pw", "user"))
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
    entry, _ = _entry_and_key(tmp_path, passphrase="secret")
    s = build_signer(entry, ALG, private_keys_dir=tmp_path,
                     passphrase_provider=lambda: ("secret", "dev"))     # source 'dev' -> is_dev_key
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


class _FakePriv:
    def __init__(self, sig):
        self._sig = sig

    def sign(self, digest):
        return self._sig


class _FakeSession:
    def __init__(self, sig, point):
        self._sig, self._point = sig, point

    def private_key(self, label):
        return _FakePriv(self._sig)

    def public_point(self, label):
        return self._point


def test_pkcs11_signer_via_fake_session():
    from openmv_ota.ota import signer_pkcs11
    entry = TrustedKey(key_id=0x0100, alg=ES256, role="ota", pubkey="")
    session = _FakeSession(b"\x11" * ALG.sig_size, b"\x04" + b"\xab" * 64)
    s = signer_pkcs11.build(entry, ALG, {"object_label": "k"}, session=session)
    assert s.sign(b"a region") == b"\x11" * ALG.sig_size
    assert s.public_point_hex() == "04" + "ab" * 64


def test_pkcs11_signer_bad_sig_length():
    from openmv_ota.ota import signer_pkcs11
    entry = TrustedKey(key_id=1, alg=ES256, role="ota", pubkey="")
    s = signer_pkcs11.build(entry, ALG, {}, session=_FakeSession(b"\x00" * 10, b"\x04\xab"))
    with pytest.raises(OtaError, match="expected"):
        s.sign(b"x")


def test_build_signer_pkcs11_dispatch(tmp_path, monkeypatch):
    from openmv_ota.ota import signer_pkcs11
    entry = TrustedKey(key_id=1, alg=ES256, role="ota", pubkey="")
    sentinel = _FakeSigner()
    monkeypatch.setattr(signer_pkcs11, "build", lambda e, a, b: sentinel)
    assert build_signer(entry, ALG, private_keys_dir=tmp_path,
                        backend={"backend": "pkcs11"}) is sentinel


class _FakeKmsClient:
    def __init__(self, sig, point):
        self._sig, self._point = sig, point

    def sign(self, region, alg):
        return self._sig

    def public_point_hex(self):
        return self._point


def test_kms_signer_via_fake_client():
    from openmv_ota.ota import signer_kms
    entry = TrustedKey(key_id=0x0100, alg=ES256, role="ota", pubkey="")
    client = _FakeKmsClient(b"\x22" * ALG.sig_size, "04" + "cd" * 64)
    s = signer_kms.build(entry, ALG, {"backend": "aws-kms"}, client=client)
    assert s.sign(b"a region") == b"\x22" * ALG.sig_size and s.public_point_hex() == "04" + "cd" * 64


def test_kms_signer_bad_sig_length():
    from openmv_ota.ota import signer_kms
    entry = TrustedKey(key_id=1, alg=ES256, role="ota", pubkey="")
    s = signer_kms.build(entry, ALG, {"backend": "aws-kms"}, client=_FakeKmsClient(b"\x00" * 3, ""))
    with pytest.raises(OtaError, match="expected"):
        s.sign(b"x")


def test_build_signer_kms_dispatch(tmp_path, monkeypatch):
    from openmv_ota.ota import signer_kms
    entry = TrustedKey(key_id=1, alg=ES256, role="ota", pubkey="")
    sentinel = _FakeSigner()
    monkeypatch.setattr(signer_kms, "build", lambda e, a, b: sentinel)
    for tag in ("aws-kms", "gcp-kms", "azure-kms"):
        assert build_signer(entry, ALG, private_keys_dir=tmp_path,
                            backend={"backend": tag}) is sentinel


def test_custom_signer_bad_ref(tmp_path):
    entry, _ = _entry_and_key(tmp_path)
    with pytest.raises(OtaError, match="needs a 'factory'"):
        build_signer(entry, ALG, private_keys_dir=tmp_path, backend={"backend": "custom"})
    with pytest.raises(OtaError, match="not importable"):
        build_signer(entry, ALG, private_keys_dir=tmp_path,
                     backend={"backend": "custom", "factory": "no_such_pkg.mod:x"})


class _FakeProvisioner:
    def __init__(self):
        self.calls = []

    def provision(self, key_id, role, alg):
        self.calls.append((key_id, role))
        return "04%02x" % (key_id & 0xFF), {"backend": "pkcs11", "object_label": "%s-%04x" % (role, key_id)}


def test_provision_external_key_set():
    from openmv_ota.ota.keys import FACTORY_KEY_ID_BASE, OTA_KEY_ID_BASE
    from openmv_ota.ota.signer import provision_external_key_set
    fake = _FakeProvisioner()
    trusted, records, signing = provision_external_key_set(ALG, 2, 3, fake)
    assert [k.role for k in trusted] == ["factory", "factory", "ota", "ota", "ota"]
    assert [k.key_id for k in trusted] == [
        FACTORY_KEY_ID_BASE, FACTORY_KEY_ID_BASE + 1,
        OTA_KEY_ID_BASE, OTA_KEY_ID_BASE + 1, OTA_KEY_ID_BASE + 2]
    assert signing == OTA_KEY_ID_BASE
    assert set(records) == {k.key_id for k in trusted}
    assert all(records[k.key_id]["object_label"].endswith("%04x" % k.key_id) for k in trusted)
    assert len(fake.calls) == 5


def test_build_provisioner_dispatch(monkeypatch):
    from openmv_ota.ota import signer, signer_kms, signer_pkcs11
    sentinel = _FakeProvisioner()
    monkeypatch.setattr(signer_pkcs11, "provisioner", lambda backend: sentinel)
    monkeypatch.setattr(signer_kms, "provisioner", lambda backend: sentinel)
    assert signer.build_provisioner({"backend": "pkcs11"}) is sentinel
    for tag in ("aws-kms", "gcp-kms", "azure-kms"):
        assert signer.build_provisioner({"backend": tag}) is sentinel


def test_build_provisioner_rejects_local_and_unknown():
    from openmv_ota.ota.signer import build_provisioner
    with pytest.raises(OtaError, match="can't provision keys externally"):
        build_provisioner({"backend": "encrypted-pem"})
    with pytest.raises(OtaError, match="can't provision keys externally"):
        build_provisioner({})
    with pytest.raises(OtaError, match="unknown provisioning backend"):
        build_provisioner({"backend": "nope"})

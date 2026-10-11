"""Seals and lockboxes: a release's content key, for keykeeper and for one camera.

The lockbox is opened by camera code (``trust.open_lockbox``: hand-rolled HMAC/HKDF over
``hashlib``, ``cryptolib`` AES), so every lockbox test opens it with that code as well as the
host's, the way a real camera meets keykeeper.
"""

from __future__ import annotations

import secrets

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from openmv_ota.build.device import openmv_se
from openmv_ota.build.device.openmv_se import trust
from openmv_ota.ota import lockbox as lb


def _point(key):
    return key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)


class _Camera:
    """A camera's exchange key, as openmv_se's drivers expose it."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())

    def ecdh(self, peer):
        return self.key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), bytes(peer)))

    def public(self):
        return _point(self.key)


class _Aes:
    """``cryptolib.aes(key, 2, iv)`` -- CBC decrypt -- on the host."""

    def __init__(self, key, mode, iv):
        assert mode == 2
        self.dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()

    def decrypt(self, data):
        return self.dec.update(data) + self.dec.finalize()


def test_a_lockbox_opens_on_its_camera_and_nowhere_else():
    cam, other = _Camera(), _Camera()
    key = secrets.token_bytes(32)
    box = lb.make_lockbox(key, cam.public().hex())
    assert len(box) == lb.LOCKBOX_SIZE == trust.LOCKBOX_SIZE
    assert trust.open_lockbox(box, cam, _Aes) == key           # the camera's own code
    assert lb.open_lockbox(box, cam.key) == key                    # the host twin
    assert lb.open_lockbox(box, cam) == key                        # an ecdh()-object key
    with pytest.raises(ValueError, match="isn't for this camera"):
        trust.open_lockbox(box, other, _Aes)
    with pytest.raises(lb.LockboxError, match="isn't for this camera"):
        lb.open_lockbox(box, other.key)


@pytest.mark.parametrize("at", [70, 90, 120])
def test_an_altered_lockbox_never_gives_a_key(at):
    cam = _Camera()
    box = bytearray(lb.make_lockbox(secrets.token_bytes(32), cam.public()))
    box[at] ^= 1
    with pytest.raises(ValueError):
        trust.open_lockbox(bytes(box), cam, _Aes)


def test_the_camera_s_hkdf_and_hmac_are_the_standard_ones():
    import hashlib
    import hmac
    secret = secrets.token_bytes(32)
    assert trust._hkdf64(secret, b"info") == lb.hkdf(secret, b"info", 64)
    assert trust._hmac(b"k" * 32, b"msg") == hmac.new(b"k" * 32, b"msg",
                                                          hashlib.sha256).digest()


def test_wrong_sizes_and_keys_are_refused(monkeypatch):
    cam = _Camera()
    with pytest.raises(ValueError, match="145"):
        trust.open_lockbox(b"\0" * 10, cam, _Aes)
    with pytest.raises(lb.LockboxError, match="is 145 bytes, not 10"):
        lb.open_lockbox(b"\0" * 10, cam.key)
    with pytest.raises(lb.LockboxError, match="content key must be 32"):
        lb.make_lockbox(b"short", cam.public())
    with pytest.raises(lb.LockboxError, match="exchange key isn't"):
        lb.make_lockbox(bytes(32), "04abcd")
    monkeypatch.setattr(trust, "_keys", None)
    monkeypatch.setattr(openmv_se, "open", lambda: None)
    with pytest.raises(ValueError, match="no keys"):
        trust.open_lockbox(bytes(145))


def test_the_camera_opens_its_keys_once_and_uses_cryptolib(monkeypatch):
    import sys
    import types
    cam, opened = _Camera(), []
    monkeypatch.setattr(trust, "_keys", None)
    monkeypatch.setattr(openmv_se, "open", lambda: opened.append(1) or cam)
    monkeypatch.setitem(sys.modules, "cryptolib", types.SimpleNamespace(aes=_Aes))
    key = secrets.token_bytes(32)
    for _ in range(2):
        assert trust.open_lockbox(lb.make_lockbox(key, cam.public())) == key
    assert opened == [1]


def test_a_seal_opens_only_with_its_keykeeper_key():
    kk, other = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    contents = {"account": "acct_a", "content_key": "ab" * 32}
    sealed = lb.seal(contents, _point(kk).hex(), 3)
    assert lb.seal_key_id(sealed) == 3 and "acct_a" not in str(sealed)
    assert lb.open_seal(sealed, kk) == contents
    with pytest.raises(lb.LockboxError, match="doesn't open"):
        lb.open_seal(sealed, other)
    moved = dict(sealed, key_id=4)                       # the key id is bound in
    with pytest.raises(lb.LockboxError, match="doesn't open"):
        lb.open_seal(moved, kk)
    flipped = dict(sealed, ct=("00" if sealed["ct"][:2] != "00" else "11") + sealed["ct"][2:])
    with pytest.raises(lb.LockboxError, match="doesn't open"):
        lb.open_seal(flipped, kk)


@pytest.mark.parametrize("sealed, why", [
    ({"v": 2, "key_id": 1}, "version"),
    ({"v": 1}, "malformed"),
    ({"v": 1, "key_id": 1, "eph": "zz", "nonce": "00", "ct": "00"}, "malformed"),
    ("not a dict", "malformed"),
])
def test_a_malformed_seal_says_so(sealed, why):
    with pytest.raises(lb.LockboxError, match=why):
        lb.open_seal(sealed, ec.generate_private_key(ec.SECP256R1()))


def test_a_seal_must_hold_a_json_object():
    import json

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    kk = ec.generate_private_key(ec.SECP256R1())
    for plain, why in ((b"not json", "aren't JSON"), (json.dumps([1]).encode(), "JSON object")):
        eph = ec.generate_private_key(ec.SECP256R1())
        eph_pub = _point(eph)
        nonce = bytes(12)
        key = lb._seal_key(eph.exchange(ec.ECDH(), kk.public_key()), 1)
        ct = AESGCM(key).encrypt(nonce, plain, lb.SEAL_INFO + eph_pub)
        sealed = {"v": 1, "key_id": 1, "eph": eph_pub.hex(), "nonce": nonce.hex(), "ct": ct.hex()}
        with pytest.raises(lb.LockboxError, match=why):
            lb.open_seal(sealed, kk)


def test_a_release_seal_carries_what_keykeeper_checks():
    kk = ec.generate_private_key(ec.SECP256R1())
    key = secrets.token_bytes(32)
    desks = [{"key_id": 1, "pubkey": "04ab", "expires": "2027-01-01T00:00:00Z"}]
    sealed = lb.seal_release(key, account="acct_a", desks=desks, keykeeper_public=_point(kk),
                             key_id=1, release="rel1")
    contents = lb.open_seal(sealed, kk)
    assert contents == {"v": 1, "account": "acct_a", "content_key": key.hex(), "release": "rel1",
                        "desks": [dict(desks[0], revoked=False)]}
    for kw, why in (({"content_key": b"x"}, "content key must be 32"),
                    ({"account": ""}, "the account"),
                    ({"desks": []}, "at least one desk key")):
        args = dict(content_key=key, account="acct_a", desks=desks, keykeeper_public=_point(kk),
                    key_id=1)
        args.update(kw)
        ck = args.pop("content_key")
        with pytest.raises(lb.LockboxError, match=why):
            lb.seal_release(ck, **args)

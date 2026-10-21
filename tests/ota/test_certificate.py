"""Tests for the signing-key certificate codec (openmv_ota.ota.certificate)."""

from __future__ import annotations

import struct

import pytest

from openmv_ota.ota import ES256, ES384, keys
from openmv_ota.ota import certificate as cert_mod
from openmv_ota.ota.algorithms import algorithm_for
from openmv_ota.ota.errors import OtaError

_SPEC = algorithm_for(ES256)


def _keypair():
    priv = keys.generate_private_key(_SPEC)
    return priv, bytes.fromhex(keys.public_point_hex(priv.public_key()))


@pytest.fixture
def root():
    return _keypair()


@pytest.fixture
def signing():
    return _keypair()


def _cert(signing_pub, **over):
    fields = dict(kind=cert_mod.KIND_OTA, root_id=0, epoch=5, pubkey=signing_pub,
                  signature=b"\x11" * 64)
    fields.update(over)
    return cert_mod.Certificate(**fields)


# --- layout -----------------------------------------------------------------

def test_size_and_signed_region_are_fixed():
    assert cert_mod.SIZE == 160 and cert_mod.SIGNED_SIZE == 96
    assert struct.calcsize(cert_mod._STRUCT) == 160


def test_round_trip(signing):
    c = _cert(signing[1])
    out = cert_mod.pack(c)
    assert len(out) == cert_mod.SIZE
    assert cert_mod.parse(out) == c


def test_field_offsets_match_the_design(signing):
    out = cert_mod.pack(_cert(signing[1], kind=1, root_id=1, epoch=0x0A0B0C0D))
    assert out[0:4] == b"OMVC"
    assert struct.unpack_from("<I", out, 4)[0] == cert_mod.VERSION
    assert struct.unpack_from("<I", out, 8)[0] == 1            # kind
    assert struct.unpack_from("<I", out, 12)[0] == 1           # root_id
    assert struct.unpack_from("<I", out, 16)[0] == 0x0A0B0C0D  # epoch
    assert struct.unpack_from("<i", out, 20)[0] == ES256       # alg
    assert out[24:89] == signing[1]                            # pubkey
    assert out[89:96] == b"\x00" * 7                           # pad
    assert out[96:160] == b"\x11" * 64                         # signature


def test_signed_region_from_certificate_and_from_bytes(signing):
    c = _cert(signing[1])
    out = cert_mod.pack(c)
    assert cert_mod.signed_region(c) == out[:96] == cert_mod.signed_region(out)
    assert cert_mod.signed_region(bytearray(out)) == out[:96]


def test_signed_region_bytes_too_short():
    with pytest.raises(OtaError, match="too small"):
        cert_mod.signed_region(b"\x00" * 95)


# --- pack rejects -------------------------------------------------------------

def test_pack_bad_signature_length(signing):
    with pytest.raises(OtaError, match="signature must be 64"):
        cert_mod.pack(_cert(signing[1], signature=b"\x00" * 63))


def test_pack_rejects_a_wider_curve(signing):
    with pytest.raises(OtaError, match="P-256 keys only"):
        cert_mod.pack(_cert(signing[1], alg=ES384))


def test_pack_rejects_unknown_kind(signing):
    with pytest.raises(OtaError, match="unknown certificate kind"):
        cert_mod.pack(_cert(signing[1], kind=2))


@pytest.mark.parametrize("pubkey", [b"\x04" * 64, b"\x02" + b"\x00" * 64])
def test_pack_rejects_a_bad_point(pubkey):
    with pytest.raises(OtaError, match="uncompressed P-256 point"):
        cert_mod.pack(_cert(pubkey))


# --- parse rejects ------------------------------------------------------------

def test_parse_too_small():
    with pytest.raises(OtaError, match="too small"):
        cert_mod.parse(b"\x00" * 159)


def _raw(signing_pub, **over):
    f = dict(magic=b"OMVC", version=cert_mod.VERSION, kind=0, root_id=0, epoch=0, alg=ES256,
             pubkey=signing_pub, sig=b"\x00" * 64)
    f.update(over)
    return struct.pack(cert_mod._STRUCT, f["magic"], f["version"], f["kind"], f["root_id"],
                       f["epoch"], f["alg"], f["pubkey"], f["sig"])


def test_parse_bad_magic(signing):
    with pytest.raises(OtaError, match="bad certificate magic"):
        cert_mod.parse(_raw(signing[1], magic=b"OMVX"))


def test_parse_bad_version(signing):
    with pytest.raises(OtaError, match="unsupported certificate version"):
        cert_mod.parse(_raw(signing[1], version=2))


def test_parse_bad_alg(signing):
    with pytest.raises(OtaError, match="P-256 keys only"):
        cert_mod.parse(_raw(signing[1], alg=ES384))


def test_parse_bad_kind(signing):
    with pytest.raises(OtaError, match="unknown certificate kind"):
        cert_mod.parse(_raw(signing[1], kind=7))


def test_parse_bad_point(signing):
    with pytest.raises(OtaError, match="uncompressed P-256 point"):
        cert_mod.parse(_raw(b"\x03" + signing[1][1:]))


# --- issue / verify -----------------------------------------------------------

def test_issue_then_verify(root, signing):
    c = cert_mod.issue(root[0], 1, cert_mod.KIND_FACTORY, 9, signing[1])
    out = cert_mod.pack(c)
    got = cert_mod.verify(out, {1: root[1]})
    assert got == c
    assert got.kind == cert_mod.KIND_FACTORY and got.root_id == 1 and got.epoch == 9
    assert got.pubkey == signing[1]


def test_verify_accepts_a_memoryview(root, signing):
    out = cert_mod.pack(cert_mod.issue(root[0], 0, cert_mod.KIND_OTA, 1, signing[1]))
    assert cert_mod.verify(memoryview(out), {0: root[1]}).epoch == 1


def test_verify_unknown_root(root, signing):
    out = cert_mod.pack(cert_mod.issue(root[0], 0, cert_mod.KIND_OTA, 1, signing[1]))
    with pytest.raises(OtaError, match="unknown root 0"):
        cert_mod.verify(out, {1: root[1]})


def test_verify_wrong_root_key(root, signing):
    other = _keypair()[1]
    out = cert_mod.pack(cert_mod.issue(root[0], 0, cert_mod.KIND_OTA, 1, signing[1]))
    with pytest.raises(OtaError, match="does not verify against root 0"):
        cert_mod.verify(out, {0: other})


@pytest.mark.parametrize("offset", [8, 12, 16, 24, 88])   # kind, root_id, epoch, pubkey start/end
def test_verify_detects_a_tampered_field(root, signing, offset):
    out = bytearray(cert_mod.pack(cert_mod.issue(root[0], 0, cert_mod.KIND_OTA, 1, signing[1])))
    out[offset] ^= 0x01
    with pytest.raises(OtaError):             # a structural reject or a signature failure
        cert_mod.verify(bytes(out), {0: root[1]})


def test_verify_still_parses_structurally_first(root):
    with pytest.raises(OtaError, match="bad certificate magic"):
        cert_mod.verify(b"\x00" * 160, {0: root[1]})


# --- key ids ------------------------------------------------------------------

def test_key_id_is_31_bits_and_matches_the_desk_key_rule(signing):
    from openmv_ota.project.desk_keys import id_for
    kid = cert_mod.key_id_of(signing[1])
    assert 0 < kid < 2 ** 31
    assert kid == id_for(signing[1].hex())
    assert _cert(signing[1]).key_id == kid


def test_key_id_is_never_zero(monkeypatch):
    class _Zero:
        def digest(self):
            return b"\x80\x00\x00\x00" + b"\x00" * 28      # top bit only -> masked to 0
    monkeypatch.setattr(cert_mod.hashlib, "sha256", lambda _b: _Zero())
    assert cert_mod.key_id_of(b"\x04" * 65) == 1

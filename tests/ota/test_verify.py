"""Tests for host-side OTA image verification (verify_image)."""

from __future__ import annotations

from openmv_ota.ota import certificate as cert_mod
from openmv_ota.ota import keys
from openmv_ota.ota.verify import verify_image
from tests import trust


def _signed(**kw):
    """Return (body, trailer_bytes, signing private key) for a freshly signed image."""
    priv = keys.generate_private_key(trust.SPEC)
    body = b"ROMFS-BODY" * 40
    return body, trust.trailer(priv, body, meta={"product": "p", "app_version": "1.2.3"},
                               product_id=7, **kw), priv


def test_verify_ok():
    body, trailer, priv = _signed(kind=cert_mod.KIND_FACTORY, epoch=3)
    ok, reason = verify_image(body, trailer, trust.ROOTS)
    assert ok and "body sha256 OK" in reason
    assert "0x%08x" % cert_mod.key_id_of(trust.pub_bytes(priv)) in reason
    assert "root 0, epoch 3" in reason


def test_verify_takes_the_committed_root_list_too():
    body, trailer, _priv = _signed()
    assert verify_image(body, trailer, trust.root_keys())[0]


def test_verify_unknown_root():
    body, trailer, _priv = _signed(root_id=1)
    ok, reason = verify_image(body, trailer, trust.ROOTS)
    assert not ok and "untrusted certificate" in reason and "unknown root 1" in reason


def test_verify_certificate_from_another_root():
    other_root = keys.generate_private_key(trust.SPEC)
    body, trailer, _priv = _signed(root_priv=other_root)
    ok, reason = verify_image(body, trailer, trust.ROOTS)
    assert not ok and "does not verify against root 0" in reason


def test_verify_bad_signature():
    # A good certificate for one key, a signature made with another.
    other = keys.generate_private_key(trust.SPEC)
    body, trailer, _priv = _signed(cert_priv=other)
    ok, reason = verify_image(body, trailer, trust.ROOTS)
    assert not ok and "signature does not verify" in reason


def test_verify_body_shorter_than_the_trailer_says():
    body, trailer, _priv = _signed()
    ok, reason = verify_image(body[:-1], trailer, trust.ROOTS)
    assert not ok and "body is" in reason


def _with_component():
    """An image whose slot carries a firmware component after the filesystem."""
    from openmv_ota.ota import components as C
    priv = keys.generate_private_key(trust.SPEC)
    body = b"ROMFS-BODY" * 40
    fw = bytes(range(256)) * 20
    image, comps = C.append(body, [(0x70090000, len(fw), C.compress(fw))])
    return image, trust.trailer(priv, body, components=comps), comps


def test_verify_checks_the_components_past_the_filesystem():
    image, trailer, comps = _with_component()
    ok, reason = verify_image(image, trailer, trust.ROOTS)
    assert ok and "1 component(s) OK" in reason
    tampered = bytearray(image)
    tampered[comps[0].offset] ^= 0x80
    ok, reason = verify_image(bytes(tampered), trailer, trust.ROOTS)
    assert not ok and "hash mismatch" in reason and "component" in reason
    ok, reason = verify_image(image[:comps[0].offset + 4], trailer, trust.ROOTS)
    assert not ok and "runs past" in reason


def test_verify_body_hash_mismatch():
    body, trailer, _priv = _signed()
    tampered = bytearray(body)
    tampered[0] ^= 0xFF  # same length, different content
    ok, reason = verify_image(bytes(tampered), trailer, trust.ROOTS)
    assert not ok and "hash mismatch" in reason


def test_verify_invalid_trailer():
    body, _trailer, _priv = _signed()
    ok, reason = verify_image(body, b"not-a-trailer", trust.ROOTS)
    assert not ok and "invalid trailer" in reason

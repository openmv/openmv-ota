"""Tests for the signed OTA update-manifest codec + policy (openmv_ota.ota.manifest)
and its host-side verification (openmv_ota.ota.verify.verify_manifest)."""

from __future__ import annotations

import binascii
import struct

import pytest

from openmv_ota.ota import ES256, ES384, algorithm_for
from openmv_ota.ota import certificate as C
from openmv_ota.ota.errors import OtaError
from openmv_ota.ota.keys import RootKey, generate_private_key, public_point_hex
from openmv_ota.ota.manifest import (
    BODY_OFFSET,
    CERT_SIZE,
    HEADER_STRUCT,
    HEADER_SIZE,
    MAGIC,
    Manifest,
    pack_manifest,
    parse_manifest,
    select_representation,
    signed_region,
    update_reject_reason,
)
from openmv_ota.ota.sign import sign_region
from openmv_ota.ota.verify import verify_manifest
from openmv_ota.ota.version import encode_app_version
from tests import trust


def _body(**over):
    body = {
        "schema": 1,
        "product_id": 7,
        "product": "OPENMV_N6",
        "version": "2.1.0",
        "payload_version": encode_app_version("2.1.0"),
        "min_platform_version": 0,
        "size": 4 * 4096,
        "sha256": "ab" * 32,
        "representations": [
            {"format": "full", "url": "https://dl.x.io/n6-v2.img.gz", "size": 900000},
        ],
    }
    body.update(over)
    return body


def _make(body=None, alg=ES256, cert=None, **cert_kw):
    """A freshly signed manifest -> (manifest_bytes, pubkey_hex). The certificate is for the
    signing key, from ``tests.trust``'s root, unless ``cert`` (packed bytes) says otherwise."""
    spec = algorithm_for(alg)
    priv = generate_private_key(spec)
    if cert is None:                    # certified keys are P-256; a P-384 signer gets a stand-in
        cert_priv = priv if alg == ES256 else generate_private_key(algorithm_for(ES256))
        cert = trust.certificate(cert_priv, **cert_kw)
    m = Manifest(body=_body() if body is None else body, certificate=cert, sig_alg=alg)
    m.signature = sign_region(priv, signed_region(m), spec)
    return pack_manifest(m), public_point_hex(priv.public_key())


def _roots(**over):
    roots = dict(trust.ROOTS)
    roots.update(over)
    return roots


# --- codec roundtrip --------------------------------------------------------

def test_roundtrip():
    raw, pub = _make()
    m = parse_manifest(raw)
    assert m.body == _body()
    assert m.sig_alg == ES256
    assert len(m.signature) == 64
    assert C.parse(m.certificate).pubkey.hex() == pub         # the signer's own certificate
    assert m.key_id == C.key_id_of(bytes.fromhex(pub))        # ...and its id derives from it


def test_signed_region_matches_between_object_and_bytes():
    raw, _pub = _make()
    m = parse_manifest(raw)
    assert signed_region(m) == signed_region(raw)            # same bytes both ways
    body_size = struct.unpack_from("<I", raw, 8)[0]
    assert signed_region(raw) == raw[: BODY_OFFSET + body_size]   # header + certificate + body
    assert signed_region(raw)[HEADER_SIZE:BODY_OFFSET] == m.certificate


# --- codec rejections -------------------------------------------------------

def test_parse_too_small():
    with pytest.raises(OtaError, match="too small"):
        parse_manifest(b"\x00" * 4)


def test_parse_bad_magic():
    raw = bytearray(_make()[0])
    raw[0:4] = b"XXXX"
    with pytest.raises(OtaError, match="bad magic"):
        parse_manifest(bytes(raw))


def test_parse_unsupported_header_version():
    m = Manifest(body=_body(), certificate=trust.certificate(generate_private_key(algorithm_for(ES256))),
                 sig_alg=ES256, signature=b"\x00" * 64, header_version=1)   # v1 named a key id
    with pytest.raises(OtaError, match="header_version"):
        parse_manifest(pack_manifest(m))


def _resealed(raw: bytes) -> bytes:
    """``raw`` with its trailing crc32 recomputed, so an edit reaches the check after the crc."""
    return raw[:-4] + struct.pack("<I", binascii.crc32(raw[:-4]) & 0xFFFFFFFF)


def test_parse_wrong_cert_size():
    raw = bytearray(_make()[0])
    struct.pack_into("<I", raw, 16, CERT_SIZE - 1)
    with pytest.raises(OtaError, match="cert_size"):
        parse_manifest(_resealed(bytes(raw)))


def test_parse_malformed_certificate():
    raw = bytearray(_make()[0])
    raw[HEADER_SIZE:HEADER_SIZE + 4] = b"XXXX"                 # the certificate's magic
    with pytest.raises(OtaError, match="manifest certificate"):
        parse_manifest(_resealed(bytes(raw)))


def test_pack_refuses_a_malformed_certificate():
    m = Manifest(body=_body(), certificate=b"\x00" * CERT_SIZE, sig_alg=ES256,
                 signature=b"\x00" * 64)
    with pytest.raises(OtaError, match="manifest certificate"):
        pack_manifest(m)
    m = Manifest(body=_body(), certificate=b"\x00" * 10, sig_alg=ES256, signature=b"\x00" * 64)
    with pytest.raises(OtaError, match="must be %d bytes" % CERT_SIZE):
        pack_manifest(m)


def test_parse_sig_size_disagrees_with_alg():
    # valid ES256 framing, but rewrite sig_alg to ES384 (sig_size 64 != 96) -> rejected
    # before the crc check (alg/size is validated first).
    raw = bytearray(_make()[0])
    struct.pack_into("<i", raw, struct.calcsize("<4sIIII"), ES384)
    with pytest.raises(OtaError, match="sig_size"):
        parse_manifest(bytes(raw))


def test_parse_unknown_algorithm():
    raw = bytearray(_make()[0])
    struct.pack_into("<i", raw, struct.calcsize("<4sIIII"), -99)
    with pytest.raises(OtaError, match="COSE"):
        parse_manifest(bytes(raw))


def test_parse_truncated():
    raw = _make()[0]
    with pytest.raises(OtaError, match="truncated"):
        parse_manifest(raw[:-1])


def test_parse_bad_crc():
    raw = bytearray(_make()[0])
    raw[-1] ^= 0xFF
    with pytest.raises(OtaError, match="crc32"):
        parse_manifest(bytes(raw))


def test_parse_non_json_body():
    # Hand-frame a structurally valid ES256 manifest whose body bytes aren't JSON.
    body = b"not json"
    sig = b"\x00" * 64
    header = struct.pack(HEADER_STRUCT, MAGIC, 2, len(body), 64, CERT_SIZE, ES256)
    framed = header + trust.certificate(generate_private_key(algorithm_for(ES256))) + body + sig
    raw = framed + struct.pack("<I", binascii.crc32(framed) & 0xFFFFFFFF)
    with pytest.raises(OtaError, match="not valid JSON"):
        parse_manifest(raw)


def test_signed_region_bytes_out_of_bounds():
    header = struct.pack(HEADER_STRUCT, MAGIC, 2, 9999, 64, CERT_SIZE, ES256)
    with pytest.raises(OtaError, match="exceeds"):
        signed_region(header)


def test_signed_region_bytes_too_small():
    with pytest.raises(OtaError, match="too small"):
        signed_region(b"\x00" * 4)


def test_pack_wrong_signature_size():
    m = Manifest(body=_body(), certificate=trust.certificate(generate_private_key(algorithm_for(ES256))),
                 sig_alg=ES256, signature=b"\x00" * 10)
    with pytest.raises(OtaError, match="signature must be 64"):
        pack_manifest(m)


def test_pack_over_size_limit():
    big = _body(blob="z" * 9000)                            # body alone exceeds the cap
    m = Manifest(body=big, certificate=trust.certificate(generate_private_key(algorithm_for(ES256))),
                 sig_alg=ES256, signature=b"\x00" * 64)
    with pytest.raises(OtaError, match="over the"):
        pack_manifest(m)


# --- verify_manifest --------------------------------------------------------

def test_verify_ok():
    raw, pub = _make(epoch=3)
    ok, reason = verify_manifest(raw, _roots())
    assert ok and "ES256" in reason and "root 0" in reason and "epoch 3" in reason
    assert ("0x%08x" % C.key_id_of(bytes.fromhex(pub))) in reason
    # a RootKey list (keys/root_keys.json) is accepted too
    assert verify_manifest(raw, trust.root_keys())[0]


def test_verify_invalid_manifest():
    ok, reason = verify_manifest(b"\x00" * 8, _roots())
    assert not ok and "invalid manifest" in reason


def test_verify_unknown_root():
    raw, _pub = _make(root_id=1)                             # certified by a root we don't embed
    ok, reason = verify_manifest(raw, _roots())
    assert not ok and "untrusted certificate" in reason and "unknown root 1" in reason


def test_verify_certificate_not_signed_by_our_root():
    rogue = generate_private_key(algorithm_for(ES256))
    raw, _pub = _make(root_priv=rogue)                       # an attacker's own "root"
    ok, reason = verify_manifest(raw, _roots())
    assert not ok and "untrusted certificate" in reason and "does not verify against root" in reason


def test_verify_algorithm_mismatch():
    raw, _pub = _make(alg=ES384)                             # a P-384 signature under a P-256 cert
    ok, reason = verify_manifest(raw, _roots())
    assert not ok and "algorithm mismatch" in reason


def test_verify_bad_signature():
    other = trust.certificate(generate_private_key(algorithm_for(ES256)))
    raw, _pub = _make(cert=other)                            # genuine cert, for a different key
    ok, reason = verify_manifest(raw, _roots())
    assert not ok and "does not verify against key" in reason


def test_verify_wrong_root_key_bytes():
    raw, _pub = _make()
    rogue = public_point_hex(generate_private_key(algorithm_for(ES256)).public_key())
    ok, reason = verify_manifest(raw, [RootKey(0, ES256, rogue)])
    assert not ok and "untrusted certificate" in reason


# --- update_reject_reason (device-relative pre-flight) ----------------------

def test_reject_schema():
    assert update_reject_reason(_body(schema=2), product_id=7, platform_version=0,
                                rollback_floor=0) == "schema"


def test_reject_board_mismatch():
    assert update_reject_reason(_body(product_id=9), product_id=7, platform_version=0,
                                rollback_floor=0) == "board"


def test_reject_account_mismatch():
    # a device with a baked account rejects a manifest published under a different account
    assert update_reject_reason(_body(account_id="acctB"), product_id=7, platform_version=0,
                                rollback_floor=0, account_id="acctA") == "account"
    # matching account (and the '' = self-host no-check case) both pass
    assert update_reject_reason(_body(account_id="acctA"), product_id=7, platform_version=(9 << 24),
                                rollback_floor=0, account_id="acctA") is None
    assert update_reject_reason(_body(account_id="acctB"), product_id=7, platform_version=(9 << 24),
                                rollback_floor=0, account_id="") is None


def test_reject_compat_floor():
    assert update_reject_reason(_body(min_platform_version=(6 << 24)), product_id=7,
                                platform_version=(5 << 24), rollback_floor=0) == "compat"


def test_reject_rollback():
    floor = encode_app_version("3.0.0")
    assert update_reject_reason(_body(), product_id=7, platform_version=0,
                                rollback_floor=floor) == "rollback"


def test_reject_stub_floor():
    """An image that needs a newer boot stub is refused before any download -- the stub itself
    would refuse it on boot, after the download and a failed trial. A board with no stub (None)
    cannot take a stub-gated image either."""
    gated = _body(min_stub=2)
    assert update_reject_reason(gated, product_id=7, platform_version=0, rollback_floor=0) == "stub"
    assert update_reject_reason(gated, product_id=7, platform_version=0, rollback_floor=0,
                                stub_version=1) == "stub"
    assert update_reject_reason(gated, product_id=7, platform_version=0, rollback_floor=0,
                                stub_version=2) is None
    assert update_reject_reason(_body(), product_id=7, platform_version=0, rollback_floor=0,
                                stub_version=None) is None       # no stub needed, none present


def test_accept_when_all_clear():
    assert update_reject_reason(_body(), product_id=7, platform_version=(9 << 24),
                                rollback_floor=0) is None


def test_accept_product_id_zero_disables_check():
    # device product_id 0 == "don't check": a manifest for another board still passes
    assert update_reject_reason(_body(product_id=9), product_id=0, platform_version=0,
                                rollback_floor=0) is None


# --- select_representation --------------------------------------------------

_FULL = {"format": "full", "url": "https://x/full.gz", "size": 900000}
_BASE_SHA = "ab" * 32
_DELTA = {"format": "ocdl", "url": "https://x/d.gz", "size": 40000,
          "base_payload_version": encode_app_version("1.0.0"),
          "base_body_sha256": _BASE_SHA}


def test_select_full_when_only_full():
    body = {"representations": [_FULL]}
    assert select_representation(body, delta_capable=True, golden_payload_version=0) is _FULL


def test_select_prefers_smaller_delta_when_capable_and_base_matches():
    body = {"representations": [_FULL, _DELTA]}
    got = select_representation(body, delta_capable=True,
                               golden_payload_version=encode_app_version("1.0.0"),
                               base_body_sha256=_BASE_SHA)
    assert got is _DELTA


def test_select_skips_delta_on_base_sha_mismatch():
    """Same version, different bytes -- the --allow-republish case. Version alone must not
    qualify a delta: the base identity is version AND sha."""
    body = {"representations": [_FULL, _DELTA]}
    got = select_representation(body, delta_capable=True,
                               golden_payload_version=encode_app_version("1.0.0"),
                               base_body_sha256="cd" * 32)
    assert got is _FULL


def test_select_skips_delta_missing_its_base_sha():
    """A rep that does not name its base's bytes is never applied -- clean break: the
    builder always emits the field, and the device requires it."""
    legacy = {"format": "ocdl", "url": "https://x/d.gz", "size": 40000,
              "base_payload_version": encode_app_version("1.0.0")}
    body = {"representations": [_FULL, legacy]}
    got = select_representation(body, delta_capable=True,
                               golden_payload_version=encode_app_version("1.0.0"),
                               base_body_sha256=_BASE_SHA)
    assert got is _FULL


def test_select_skips_delta_when_device_has_no_base_sha():
    """A device whose running trailer would not parse reports "" -- it takes the full
    image rather than patching bytes it cannot name."""
    body = {"representations": [_FULL, _DELTA]}
    got = select_representation(body, delta_capable=True,
                               golden_payload_version=encode_app_version("1.0.0"),
                               base_body_sha256="")
    assert got is _FULL


def test_select_falls_back_to_full_when_not_delta_capable():
    body = {"representations": [_FULL, _DELTA]}
    got = select_representation(body, delta_capable=False,
                               golden_payload_version=encode_app_version("1.0.0"))
    assert got is _FULL


def test_select_skips_delta_on_base_mismatch():
    body = {"representations": [_FULL, _DELTA]}
    got = select_representation(body, delta_capable=True,
                               golden_payload_version=encode_app_version("2.0.0"),
                               base_body_sha256=_BASE_SHA)
    assert got is _FULL


def test_select_skips_unknown_format_and_repr_without_size():
    weird = {"format": "lzma", "url": "https://x/w.gz", "size": 1}
    nosize = {"format": "full", "url": "https://x/n.gz"}     # no size -> treated as huge
    body = {"representations": [weird, nosize, _FULL]}
    assert select_representation(body, delta_capable=True, golden_payload_version=0) is _FULL


def test_select_none_when_nothing_usable():
    body = {"representations": [_DELTA]}                     # delta only, not capable
    assert select_representation(body, delta_capable=False, golden_payload_version=0) is None



# --- JSON that parses but is not a manifest body (found by tests/fuzz) -------------------

def _raw_manifest(body_bytes):
    head = struct.pack(HEADER_STRUCT, MAGIC, 2, len(body_bytes), 64, CERT_SIZE, ES256)
    cert = trust.certificate(generate_private_key(algorithm_for(ES256)))
    out = head + cert + body_bytes + b"\x00" * 64
    return out + struct.pack("<I", binascii.crc32(out) & 0xFFFFFFFF)


@pytest.mark.parametrize("body", [b"[]", b"1", b"null", b'"x"'])
def test_parse_body_that_is_not_an_object(body):
    """The server does ``body["product_id"]`` straight after parsing, so a list here was a
    500 on publish; the device's vetting does ``body.get(...)``."""
    with pytest.raises(OtaError, match="not a JSON object"):
        parse_manifest(_raw_manifest(body))


def test_parse_body_nested_past_the_recursion_limit():
    """The publish endpoint accepts a manifest up to 1 MiB; nesting that deep raised
    RecursionError (a 500) instead of OtaError (a 400)."""
    with pytest.raises(OtaError, match="not valid JSON"):
        parse_manifest(_raw_manifest(b"[" * 50_000 + b"]" * 50_000))

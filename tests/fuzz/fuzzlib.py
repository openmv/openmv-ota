"""Shared builders for the parser fuzz suite: real keys, real signed artifacts, and the device
modules loaded the way the rest of the suite loads them (file-based, so coverage sees them)."""

from __future__ import annotations

import binascii
import hashlib
import importlib.util
import struct
from pathlib import Path

from hypothesis import strategies as st

from openmv_ota.ota import keys, sign
from openmv_ota.ota import manifest as host_manifest
from openmv_ota.ota import trailer as host_trailer
from openmv_ota.ota.algorithms import ES256, ES384, ES512, algorithm_for
from openmv_ota.ota.keys import TrustedKey

_ROOT = Path(__file__).resolve().parents[2]
ALGS = (ES256, ES384, ES512)
KEY_ID = 0x0101


def load_installer():
    """The device installer, imported from its file under an ``openmv_ota.*`` name so
    ``--cov=openmv_ota`` measures it (the same trick ``tests/build/test_installer.py`` uses)."""
    src = _ROOT / "src/openmv_ota/build/device/openmv_ota/data/installer.py"
    spec = importlib.util.spec_from_file_location("openmv_ota._installer_under_fuzz", str(src))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def with_crc(body: bytes) -> bytes:
    """``body || crc32(body)`` -- the framing every signed container here ends with. A fuzzer
    that cannot forge a CRC never gets past the first check, so the structured strategies
    below always stamp a correct one: the CRC is integrity, not authenticity."""
    return body + struct.pack("<I", binascii.crc32(body) & 0xFFFFFFFF)


# --- keys -------------------------------------------------------------------------------------

_KEYS: dict = {}


def keypair(alg=ES256):
    """A (private key, uncompressed public point bytes) pair per algorithm, minted once."""
    if alg not in _KEYS:
        priv = keys.generate_private_key(algorithm_for(alg))
        _KEYS[alg] = (priv, bytes.fromhex(keys.public_point_hex(priv.public_key())))
    return _KEYS[alg]


def trusted(alg=ES256, key_id=KEY_ID):
    """The host verifier's trusted set holding just ``keypair(alg)``."""
    return [TrustedKey(key_id=key_id, alg=alg, role="ota", pubkey=keypair(alg)[1].hex())]


def device_verify(alg, pubkey_bytes, sig, msg):
    """The ``verify`` boot.py / the installer take on device (the mbedtls C module), done with
    the host's cryptography -- a real ECDSA check, never a stub."""
    spec = algorithm_for(alg)
    try:
        pub = keys.public_key_from_hex(pubkey_bytes.hex(), spec)
    except Exception:
        return False
    return sign.verify_region(pub, msg, sig, spec)


# --- artifacts --------------------------------------------------------------------------------

def signed_trailer(body: bytes, alg=ES256, key_id=KEY_ID, **fields) -> bytes:
    """A genuine trailer for ``body``, signed with ``keypair(alg)``."""
    priv, _pub = keypair(alg)
    spec = algorithm_for(alg)
    kw = dict(body_size=len(body), pad_size=0, meta={"product": "P", "account_id": "a"},
              product_id=0x1234, min_platform_version=0, payload_version=1 << 24,
              publish_seq=7, key_id=key_id, sig_alg=alg,
              body_sha256=hashlib.sha256(body).digest())
    kw.update(fields)
    t = host_trailer.Trailer(**kw)
    t.signature = sign.sign_region(priv, host_trailer.signed_region(t), spec)
    return host_trailer.pack_trailer(t)


def manifest_body(image: bytes = b"\xa5" * 64, **extra) -> dict:
    body = {"schema": 1, "product_id": 0x1234, "product": "P", "version": "2.0.0",
            "payload_version": 2 << 24, "publish_seq": 9, "min_platform_version": 0,
            "size": len(image), "sha256": hashlib.sha256(image).hexdigest(),
            "representations": [{"format": "full", "url": "x-ota.img.gz", "size": 10}]}
    body.update(extra)
    return body


def signed_manifest(body: dict, alg=ES256, key_id=KEY_ID) -> bytes:
    priv, _pub = keypair(alg)
    spec = algorithm_for(alg)
    m = host_manifest.Manifest(body=body, key_id=key_id, sig_alg=alg)
    m.signature = sign.sign_region(priv, host_manifest.signed_region(m), spec)
    return host_manifest.pack_manifest(m)


# --- mutation ---------------------------------------------------------------------------------

@st.composite
def mutated(draw, original: bytes, max_ops: int = 4):
    """``original`` after 1..``max_ops`` random edits: flip bits, overwrite, insert, delete, or
    truncate. Never returns ``original`` unchanged (a no-op draw is retried as a bit flip)."""
    data = bytearray(original)
    for _ in range(draw(st.integers(1, max_ops))):
        op = draw(st.sampled_from(("flip", "set", "insert", "delete", "truncate")))
        n = len(data)
        if op == "flip" and n:
            i = draw(st.integers(0, n - 1))
            data[i] ^= draw(st.integers(1, 255))
        elif op == "set" and n:
            i = draw(st.integers(0, n - 1))
            data[i] = draw(st.integers(0, 255))
        elif op == "insert":
            i = draw(st.integers(0, n))
            data[i:i] = draw(st.binary(min_size=1, max_size=8))
        elif op == "delete" and n:
            i = draw(st.integers(0, n - 1))
            del data[i:i + draw(st.integers(1, 8))]
        elif op == "truncate" and n:
            del data[draw(st.integers(0, n - 1)):]
    if bytes(data) == original:
        if not data:                       # only if original was empty
            return b"\x00"
        data[0] ^= 0x01
    return bytes(data)


json_scalars = (st.none() | st.booleans() | st.integers(-(1 << 70), 1 << 70)
                | st.floats(allow_nan=False) | st.text(max_size=12))
json_values = st.recursive(
    json_scalars,
    lambda kids: st.lists(kids, max_size=4) | st.dictionaries(st.text(max_size=8), kids,
                                                                max_size=4),
    max_leaves=12)

"""ECDSA key generation and the trusted-key set for OTA signing.

Host-side, built on ``cryptography``. Public keys are stored as the uncompressed
EC point in hex (``04 || X || Y``), which the device's mbedtls verifier reads
directly via ``mbedtls_ecp_point_read_binary``; private keys are unencrypted
PKCS#8 PEM.

``keys/trusted_keys.json`` is the committed public set the firmware build bakes
into ``TRUSTED_KEYS``. Each entry carries the key's id, COSE algorithm, role
(``ota`` / ``factory`` / ``emergency`` / …), and point — enough for the firmware
to verify and for the build to know which algorithm a given key signs with.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from .algorithms import AlgSpec
from .errors import OtaError

# cryptography curve objects per COSE/mbedtls curve name (supported algs only).
_CURVES = {
    "secp256r1": ec.SECP256R1,
    "secp384r1": ec.SECP384R1,
    "secp521r1": ec.SECP521R1,
}


def curve_for(alg: AlgSpec) -> ec.EllipticCurve:
    """The ``cryptography`` curve instance for an algorithm."""
    return _CURVES[alg.curve]()


def generate_private_key(alg: AlgSpec):
    """Generate an ECDSA private key on ``alg``'s curve."""
    return ec.generate_private_key(curve_for(alg))


def public_point_hex(public_key) -> str:
    """The public key as the uncompressed EC point (``04 || X || Y``) in hex."""
    point = public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return point.hex()


def public_key_from_hex(point_hex: str, alg: AlgSpec):
    """Reconstruct a public key from an uncompressed-point hex string."""
    try:
        return ec.EllipticCurvePublicKey.from_encoded_point(
            curve_for(alg), bytes.fromhex(point_hex)
        )
    except ValueError as e:
        raise OtaError("invalid public point: %s" % e) from None


def private_key_pem(private_key) -> bytes:
    """Serialize a private key as unencrypted PKCS#8 PEM."""
    return private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def load_private_key_pem(data: bytes):
    """Load a PKCS#8 PEM private key."""
    try:
        return serialization.load_pem_private_key(data, password=None)
    except ValueError as e:
        raise OtaError("could not load private key: %s" % e) from None


# --- trusted_keys.json ------------------------------------------------------

TRUSTED_KEYS_NAME = "trusted_keys.json"
TRUSTED_KEYS_SCHEMA = 1


@dataclass
class TrustedKey:
    """One public key in the committed trusted-key set."""

    key_id: int
    alg: int        # COSE algorithm id
    role: str       # "ota" | "factory" | "emergency" | ...
    pubkey: str     # uncompressed EC point, hex

    def to_dict(self) -> dict:
        return {"key_id": self.key_id, "alg": self.alg, "role": self.role, "pubkey": self.pubkey}

    @classmethod
    def from_dict(cls, d: dict) -> "TrustedKey":
        return cls(
            key_id=int(d["key_id"]), alg=int(d["alg"]), role=str(d["role"]), pubkey=str(d["pubkey"])
        )


def read_trusted_keys(path: Path) -> list[TrustedKey]:
    """Load the trusted-key set, or raise ``OtaError`` if it is missing/corrupt."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError:
        raise OtaError("no %s found" % TRUSTED_KEYS_NAME) from None
    except json.JSONDecodeError as e:
        raise OtaError("%s is not valid JSON: %s" % (TRUSTED_KEYS_NAME, e)) from None
    return [TrustedKey.from_dict(k) for k in data.get("keys", [])]


def write_trusted_keys(path: Path, keys: list[TrustedKey]) -> None:
    """Write the trusted-key set (committed, public)."""
    doc = {"schema": TRUSTED_KEYS_SCHEMA, "keys": [k.to_dict() for k in keys]}
    Path(path).write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

"""Host-side ECDSA signing and verification over a trailer's signed region.

Built on ``cryptography``. Signatures are the fixed-width raw ``R||S`` form the
trailer carries (the COSE/JOSE convention), converted from the library's DER
output; the device verifier (mbedtls) reverses the conversion. ECDSA nonce
generation is handled by the vetted backend — never hand-rolled.
"""

from __future__ import annotations

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)

from .algorithms import AlgSpec

# Message digest per algorithm (supported algs only).
_HASHES = {
    "sha256": hashes.SHA256,
    "sha384": hashes.SHA384,
    "sha512": hashes.SHA512,
}


def hash_for(alg: AlgSpec):
    """The ``cryptography`` hash instance for an algorithm."""
    return _HASHES[alg.hash_name]()


def der_to_raw(der: bytes, alg: AlgSpec) -> bytes:
    """Convert a DER-encoded ECDSA signature to the fixed-width raw ``R||S`` form the trailer
    stores. Shared by every signer backend (``cryptography``/PKCS#11/KMS all funnel through here)."""
    r, s = decode_dss_signature(der)
    n = alg.sig_size // 2
    return r.to_bytes(n, "big") + s.to_bytes(n, "big")


def raw_to_der(signature: bytes, alg: AlgSpec) -> bytes:
    """Convert a raw ``R||S`` signature back to DER (the form ``cryptography.verify`` wants)."""
    n = alg.sig_size // 2
    return encode_dss_signature(int.from_bytes(signature[:n], "big"),
                                int.from_bytes(signature[n:], "big"))


def sign_region(private_key, region: bytes, alg: AlgSpec) -> bytes:
    """Sign a trailer's signed region, returning the raw ``R||S`` signature
    (``alg.sig_size`` bytes — the form the trailer stores)."""
    return der_to_raw(private_key.sign(region, ec.ECDSA(hash_for(alg))), alg)


def verify_region(public_key, region: bytes, signature: bytes, alg: AlgSpec) -> bool:
    """Return whether the raw ``R||S`` ``signature`` is valid for ``region``."""
    if len(signature) != alg.sig_size:
        return False
    try:
        public_key.verify(raw_to_der(signature, alg), region, ec.ECDSA(hash_for(alg)))
    except InvalidSignature:
        return False
    return True

"""COSE signature-algorithm registry for OTA trailers.

Signature algorithms are named by their IANA COSE Algorithm identifier
(RFC 9053) — the same scheme the host signer and the on-device verifier use.
Each entry maps a COSE id to the mbedtls curve + hash it implies and the fixed
byte lengths of the raw ``R||S`` signature and the uncompressed public key, so
the trailer codec can validate signature lengths without doing any crypto.

The registry is exactly what both ends do: ECDSA over the NIST P-curves with
SHA-256/384/512, which the OpenMV firmware's mbedtls verifies and the host signs
with. A COSE id outside this set is rejected — supporting another curve means
wiring it on both the host and the device first.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import OtaError

# IANA COSE Algorithms registry (RFC 9053). ECDSA ids encode curve + hash.
ES256 = -7
ES384 = -35
ES512 = -36


@dataclass(frozen=True)
class AlgSpec:
    """One COSE algorithm: its curve/hash and the fixed signature/key sizes."""

    cose_id: int
    name: str
    curve: str        # mbedtls ECP group name
    hash_name: str    # message-digest name
    sig_size: int     # raw R||S signature length, bytes
    pubkey_size: int  # uncompressed public point length, bytes


_ALGORITHMS = {
    spec.cose_id: spec
    for spec in (
        AlgSpec(ES256, "ES256", "secp256r1", "sha256", 64, 65),
        AlgSpec(ES384, "ES384", "secp384r1", "sha384", 96, 97),
        AlgSpec(ES512, "ES512", "secp521r1", "sha512", 132, 133),
    )
}


def algorithm_for(cose_id: int) -> AlgSpec:
    """Return the ``AlgSpec`` for a COSE algorithm id, or raise ``OtaError`` for any
    id this registry doesn't support (ECDSA over P-256 / P-384 / P-521)."""
    spec = _ALGORITHMS.get(cose_id)
    if spec is None:
        raise OtaError("unknown COSE algorithm id %d" % cose_id)
    return spec

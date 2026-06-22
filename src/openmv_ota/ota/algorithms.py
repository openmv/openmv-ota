"""COSE signature-algorithm registry for OTA trailers.

Signature algorithms are named by their IANA COSE Algorithm identifier
(RFC 9053) — the same scheme the host signer and the on-device verifier use.
Each entry maps a COSE id to the mbedtls curve + hash it implies and the fixed
byte lengths of the raw ``R||S`` signature and the uncompressed public key, so
the trailer codec can validate signature lengths without doing any crypto.

The supported set mirrors what the OpenMV firmware's mbedtls already compiles in
(ECDSA over the NIST P-curves + secp256k1, SHA-256/384/512). Entries marked
unsupported are reserved: their ids are recognised but ``algorithm_for`` refuses
them until a device-side verifier exists.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import OtaError

# IANA COSE Algorithms registry (RFC 9053). ECDSA ids encode curve + hash.
ES256 = -7
ES384 = -35
ES512 = -36
ES256K = -47
EDDSA = -8


@dataclass(frozen=True)
class AlgSpec:
    """One COSE algorithm: its curve/hash and the fixed signature/key sizes."""

    cose_id: int
    name: str
    curve: str        # mbedtls ECP group name
    hash_name: str    # message-digest name (empty for EdDSA's internal hash)
    sig_size: int     # raw R||S signature length, bytes
    pubkey_size: int  # uncompressed public point length, bytes
    supported: bool   # False = reserved / not wired to a device verifier yet


_ALGORITHMS = {
    spec.cose_id: spec
    for spec in (
        AlgSpec(ES256, "ES256", "secp256r1", "sha256", 64, 65, True),
        AlgSpec(ES384, "ES384", "secp384r1", "sha384", 96, 97, True),
        AlgSpec(ES512, "ES512", "secp521r1", "sha512", 132, 133, True),
        AlgSpec(ES256K, "ES256K", "secp256k1", "sha256", 64, 65, False),
        AlgSpec(EDDSA, "EdDSA", "ed25519", "", 64, 32, False),
    )
}


def algorithm_for(cose_id: int) -> AlgSpec:
    """Return the ``AlgSpec`` for a COSE algorithm id.

    Raises ``OtaError`` for an unknown id, or for a recognised-but-reserved id
    (EdDSA, and ES256K until a device verifier is wired up).
    """
    spec = _ALGORITHMS.get(cose_id)
    if spec is None:
        raise OtaError("unknown COSE algorithm id %d" % cose_id)
    if not spec.supported:
        raise OtaError(
            "COSE algorithm %s (%d) is reserved / not supported yet" % (spec.name, cose_id)
        )
    return spec

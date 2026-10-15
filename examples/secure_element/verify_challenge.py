"""Check a signature from ``sign_challenge.py``, on the host: an ECDSA P-256 signature by the
camera's identity key over SHA-256 of the demo's context and the challenge. Prints OK (exit 0)
or FAILED (exit 1).

    python verify_challenge.py --public-key 04... --challenge ... --signature 30...

A good signature proves only which key made it, so compare the public key with one you already
trust for that camera: the identity key ``openmv-ota flash factory`` printed (its ``keys made:``
or ``keys present:`` line), or what ``read_keys.py`` prints. Needs ``cryptography``, which
openmv-ota installs.
"""

from __future__ import annotations

import argparse
import sys

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

CONTEXT = b"omv-demo-challenge-v1\n"      # sign_challenge.py signs SHA-256(CONTEXT + challenge)


def _hex(text: str) -> bytes:
    try:
        return bytes.fromhex(text)
    except ValueError:
        raise argparse.ArgumentTypeError("not hex: %r" % text) from None


def verify(public_key: bytes, challenge: bytes, signature: bytes) -> bool:
    """Whether ``signature`` (DER) is ``public_key``'s (65 bytes, ``04 || X || Y``) over
    SHA-256(CONTEXT + challenge). Raises ValueError if ``public_key`` isn't a P-256 point."""
    key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public_key)
    try:
        key.verify(signature, CONTEXT + challenge, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Check a signature from sign_challenge.py.")
    p.add_argument("--public-key", type=_hex, required=True,
                   help="the camera's identity key: 65 bytes of hex, 04 || X || Y")
    p.add_argument("--challenge", type=_hex, required=True, help="the challenge it signed, hex")
    p.add_argument("--signature", type=_hex, required=True, help="its DER signature, hex")
    args = p.parse_args(argv)
    try:
        ok = verify(args.public_key, args.challenge, args.signature)
    except ValueError as e:
        p.error("--public-key: %s" % e)
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

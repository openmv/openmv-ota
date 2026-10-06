"""Seed corpus for the ECDSA shim fuzzer: real signatures, so mutations start from inputs that
reach the curve arithmetic (random bytes almost never pass the on-curve check).

    python fuzz/seeds.py <out dir>

Each seed is [alg][0x00][uncompressed pub][raw R||S][message] -- see ecdsa_verify_fuzz.c --
signed by the host's ``cryptography``, the signer the update tooling itself uses.
"""
import os
import sys

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

CURVES = [(0, ec.SECP256R1(), hashes.SHA256(), 32), (1, ec.SECP384R1(), hashes.SHA384(), 48),
          (2, ec.SECP521R1(), hashes.SHA512(), 66)]


def main(out):
    os.makedirs(out, exist_ok=True)
    n = 0
    for sel, curve, digest, width in CURVES:
        key = ec.generate_private_key(curve)
        pub = key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        for msg in (b"", b"openmv-ota", os.urandom(256)):
            r, s = decode_dss_signature(key.sign(msg, ec.ECDSA(digest)))
            sig = r.to_bytes(width, "big") + s.to_bytes(width, "big")
            with open(os.path.join(out, "seed-%d-%d" % (sel, n)), "wb") as f:
                f.write(bytes([sel, 0]) + pub + sig + msg)
            n += 1
    print("%d seeds -> %s" % (n, out))


if __name__ == "__main__":
    main(sys.argv[1])

# sign_challenge.py -- sign a challenge with this camera's identity key (the one it signs its
# check-ins with) and print the host command that checks the signature: verify_challenge.py.
#
# Run it on the camera: from OpenMV IDE, or `mpremote run sign_challenge.py`. The keys are made
# once, at a desk, by `openmv-ota flash factory`.

import binascii
import hashlib

from openmv_ota import se

# The challenge, as hex: put your verifier's nonce here. Left empty, the camera draws 32 random
# bytes from its RNG instead (the secure element's, on a board with one).
CHALLENGE = ""

# What the signature covers: SHA-256 of this context, then the challenge. The context keeps a
# signature made here from ever being one the camera makes for anything else: a challenge built
# to look like a check-in still can't get a check-in signed. verify_challenge.py adds the same.
CONTEXT = b"omv-demo-challenge-v1\n"


def hexstr(b):
    return binascii.hexlify(b).decode()


def open_keys():
    """This camera's keys, or None -- after saying why -- if it has none."""
    try:
        keys = se.open()
    except OSError as e:
        # Each driver has its own NotProvisioned (an OSError). Any other OSError is damaged
        # keys, and its message says what to do.
        if type(e).__name__ != "NotProvisioned":
            raise
        print("This camera has no keys yet: `openmv-ota flash factory` makes them.")
        return None
    if keys is None:
        print("This board has no keys: its firmware carries no key store.")
    return keys


keys = open_keys()
if keys is not None:
    challenge = binascii.unhexlify(CHALLENGE) if CHALLENGE else keys.random(32)
    digest = hashlib.sha256(CONTEXT + challenge).digest()
    signature = keys.sign(digest)                        # DER ECDSA P-256 over the digest
    public_key = hexstr(keys.public_key())
    print("challenge   " + hexstr(challenge))
    print("public key  " + public_key)
    print("signature   " + hexstr(signature))
    print()
    print("Check it on the host:")
    print("python verify_challenge.py --public-key %s --challenge %s --signature %s"
          % (public_key, hexstr(challenge), hexstr(signature)))

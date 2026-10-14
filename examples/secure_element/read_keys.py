# read_keys.py -- print this camera's keys: what holds them, the identity key (the one the
# camera signs its check-ins with), the exchange key (it only does ECDH) and the chip maker's
# certificate for the identity key, where the chip has one (the SE050: NXP's).
#
# Run it on the camera: from OpenMV IDE, or `mpremote run read_keys.py`. The keys are made
# once, at a desk, by `openmv-ota flash factory`; this only reads them.

import binascii
import hashlib

from openmv_ota import se

KINDS = {                                     # the frozen module that holds this board's keys
    "se050": "NXP SE050 secure element",
    "atecc608": "Microchip ATECC608 secure element",
    "soft": "the camera's own key area (no secure element)",
}


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
    kind = type(keys).__module__.split(".")[-1]          # openmv_se.<module>
    print("keys held by  " + KINDS.get(kind, kind))
    print("identity key  " + hexstr(keys.public_key()))
    print("exchange key  " + hexstr(keys.ecdh_public_key()))
    cert = keys.certificate()                            # DER, or None
    if cert is None:
        print("certificate   none: this chip carries no maker's certificate")
    else:
        print("certificate   %d bytes, SHA-256 %s"
              % (len(cert), hexstr(hashlib.sha256(cert).digest())))
        b64 = binascii.b2a_base64(cert).decode().strip()
        print("-----BEGIN CERTIFICATE-----")
        for i in range(0, len(b64), 64):                 # PEM: 64 characters a line
            print(b64[i:i + 64])
        print("-----END CERTIFICATE-----")

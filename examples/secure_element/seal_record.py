# seal_record.py -- keep a record on the camera that nothing else can read: seal it with the
# camera's own keys (`se.seal`), open it again (`se.unseal`), and see an edited copy refused.
#
# /flash is host-mountable by design: any PC that plugs in can read and edit what is there. A
# record that must stay private is sealed, not encrypted under something guessable like the
# camera's serial number. The key never exists outside the camera: it is derived from the
# exchange key, which a secure element never shows and the N6 keeps sealed under its chip key.
#
# Run it on the camera: from OpenMV IDE, or `mpremote run seal_record.py`. The keys are made
# once, at a desk, by `openmv-ota flash factory`; this only uses them.

import json

from openmv_ota import se

RECORD = {"reading": 1234, "unit": "kWh"}     # what the application wants kept private
PURPOSE = b"records"                          # the aad: a blob opens only under its own purpose


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


if open_keys() is not None:
    data = json.dumps(RECORD).encode()
    blob = se.seal(data, aad=PURPOSE)
    print("record        %d bytes -> sealed %d bytes" % (len(data), len(blob)))
    print("opened        %r" % json.loads(se.unseal(blob, aad=PURPOSE)))
    try:
        se.unseal(blob, aad=b"other purpose")
    except ValueError as e:
        print("other aad     refused: %s" % e)
    edited = bytearray(blob)
    edited[20] ^= 0x01                       # one bit of the ciphertext, as a PC edit would
    try:
        se.unseal(edited, aad=PURPOSE)
    except ValueError as e:
        print("edited copy   refused: %s" % e)
    # To keep it: write the blob to a file like any other.
    #     with open("/flash/record.sealed", "wb") as f:
    #         f.write(blob)

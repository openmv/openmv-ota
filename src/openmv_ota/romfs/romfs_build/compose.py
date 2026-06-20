"""Trailer + slot composition (stub).

The trailer is the security-critical on-flash structure. Its exact byte layout is
pinned in the concept plan ("Trailer (immutable, written last during update)"):
a 4 KiB sector, 256 bytes used, with a 128-byte signed prefix covered by the
ed25519 signature, then the 64-byte signature, reserved bytes, and a CRC32 over
bytes [0:252].

TODO: implement compose_trailer(...) and full-slot assembly (body + 0xFF pad +
status sector + trailer), matching boot.py's parser exactly.
"""

# Pinned constants (keep in lock-step with boot.py / the plan).
TRAILER_SZ = 4096
STATUS_SZ = 4096
TRAILER_MAGIC = 0x4F4D5246  # "OMRF"

PENDING_MARK = b"\xA1" * 16
TRIED_MARK = b"\xA2" * 16
CONFIRMED_MARK = b"\xA3" * 16

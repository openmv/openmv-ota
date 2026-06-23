"""Host-side verification of a built OTA image (body + signed trailer).

This is the mirror of what the device's ``boot.py`` does for *authenticity and
integrity* — verify the signature over the trailer's signed region against a
trusted (non-revoked) key, and confirm the body matches the signed digest. It does
**not** check the device-relative fields (``board_id`` vs the device,
``payload_version`` anti-rollback vs the installed image, ``min_platform_version``
vs the running firmware) — those need a device, not a host. So this is the
pre-publish / CI gate: "is this image genuine and intact?"
"""

from __future__ import annotations

import hashlib

from .errors import OtaError


def verify_image(body: bytes, trailer_bytes: bytes, trusted: list) -> tuple[bool, str]:
    """Verify ``body`` against its ``trailer_bytes`` using the ``trusted`` key set
    (a list of :class:`~openmv_ota.ota.keys.TrustedKey`). Returns ``(ok, reason)``."""
    from .algorithms import algorithm_for
    from .keys import public_key_from_hex
    from .sign import verify_region
    from .trailer import parse_trailer, signed_region

    try:
        t = parse_trailer(trailer_bytes)
    except OtaError as e:
        return False, "invalid trailer: %s" % e

    entry = next((k for k in trusted if k.key_id == t.key_id), None)
    if entry is None:
        return False, "signed by unknown key 0x%04x (not in the trusted set)" % t.key_id
    if entry.revoked:
        return False, "signed by a revoked key 0x%04x" % t.key_id
    if entry.alg != t.sig_alg:
        return False, "algorithm mismatch: trailer is %d, key 0x%04x is %d" % (
            t.sig_alg, t.key_id, entry.alg)

    alg = algorithm_for(t.sig_alg)
    pub = public_key_from_hex(entry.pubkey, alg)
    if not verify_region(pub, signed_region(trailer_bytes), t.signature, alg):
        return False, "signature does not verify against key 0x%04x" % t.key_id

    if len(body) != t.body_size:
        return False, "body is %d bytes, trailer says %d" % (len(body), t.body_size)
    if hashlib.sha256(body).digest() != t.body_sha256:
        return False, "body hash mismatch (tampered, or wrong body for this trailer)"

    return True, "signed by key 0x%04x (%s), body sha256 OK" % (t.key_id, alg.name)

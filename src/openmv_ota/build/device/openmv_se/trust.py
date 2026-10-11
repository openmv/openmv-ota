"""``openmv_se.trust`` -- what the camera's keys are FOR, beyond proving who it is at a desk:
signing its check-ins, and opening the lockbox an update encrypted for it arrives with.

Frozen with the rest of ``openmv_se`` -- except on a board whose firmware has no room for it
(boards.json ``secure_element.trust`` false: the M4). Such a camera checks in unsigned, which a
server answers with update offers, and can't install a release encrypted for each camera.

RAM BUDGET: nothing until called; the keys are opened once and kept (``openmv_se.open``).
"""

# --- signed check-ins ----------------------------------------------------------------------
# Every check-in (the app's, and recovery's) carries the camera's signature, so the server knows
# which camera is talking: its registered identity key verifies it. The signed message binds the
# body to the time, so a captured check-in stops working minutes later.

CHECKIN_CONTEXT = b"omv-checkin-v1\n"
_keys = None                                      # opened once: a soft board scans its key area


def checkin_digest(payload, ts):
    """The 32-byte SHA-256 a check-in's signature covers: a fixed context, the unix time, the
    body. The server computes the same from what it received."""
    import hashlib
    return hashlib.sha256(CHECKIN_CONTEXT + ("%d\n" % ts).encode() + payload).digest()


def checkin_signature(payload, ts, keys=None):
    """The check-in's ``X-OMV-Signature`` value, ``v=1;ts=<unix>;sig=<DER hex>``, for ``payload``
    sent at unix time ``ts`` -- or None with no trusted time (``ts`` None: a cold boot before the
    first check-in has set the clock) or no keys. Never raises: a camera whose keys fail sends
    unsigned, and the server decides what an unsigned check-in gets."""
    global _keys
    if ts is None:
        return None
    try:
        if keys is None:
            if _keys is None:
                from . import open as open_keys     # looked up now: tests replace it
                _keys = open_keys()
            keys = _keys
        if keys is None:
            return None
        import binascii
        sig = keys.sign(checkin_digest(payload, ts))
        return "v=1;ts=%d;sig=%s" % (ts, binascii.hexlify(sig).decode())
    except Exception:
        return None


# --- per-camera image keys -----------------------------------------------------------------
# An encrypted update's content key reaches a camera as a LOCKBOX that only its exchange key
# opens (keykeeper makes it; see openmv_ota.ota.lockbox on the host):
#
#     ephemeral public key (65) || iv (16) || AES-256-CBC(k_enc, content key) (32) || HMAC (32)
#
# with k_enc || k_mac = HKDF-SHA256(ECDH(exchange key, ephemeral key), info="omv-lockbox-v1") and
# the HMAC-SHA256 over everything before it. CBC and HMAC because they are what every OpenMV
# camera has (cryptolib has no GCM). Frozen here so recovery can open one too.

LOCKBOX_INFO = b"omv-lockbox-v1"
LOCKBOX_SIZE = 145


def _hmac(key, msg):
    """HMAC-SHA256 (``key`` at most 64 bytes, which is all this module uses)."""
    import hashlib
    key = key + bytes(64 - len(key))
    inner = hashlib.sha256(bytes(b ^ 0x36 for b in key) + msg).digest()
    return hashlib.sha256(bytes(b ^ 0x5C for b in key) + inner).digest()


def _hkdf64(secret, info):
    """64 bytes of HKDF-SHA256 with no salt (RFC 5869: a salt of 32 zero bytes)."""
    prk = _hmac(bytes(32), secret)
    t1 = _hmac(prk, info + b"\x01")
    return t1 + _hmac(prk, t1 + info + b"\x02")


def open_lockbox(lockbox, keys=None, aes=None):
    """The 32-byte content key out of ``lockbox`` (145 bytes), with this camera's exchange key.
    Raises ValueError if it isn't this camera's, or was altered -- never returns a wrong key.
    ``keys``/``aes`` are injectable for tests (``aes`` defaults to ``cryptolib.aes``)."""
    global _keys
    lockbox = bytes(lockbox)
    if len(lockbox) != LOCKBOX_SIZE:
        raise ValueError("a lockbox is %d bytes, not %d" % (LOCKBOX_SIZE, len(lockbox)))
    if keys is None:
        if _keys is None:
            from . import open as open_keys
            _keys = open_keys()
        keys = _keys
    if keys is None:
        raise ValueError("this camera has no keys to open a lockbox with")
    okm = _hkdf64(bytes(keys.ecdh(lockbox[:65])), LOCKBOX_INFO)
    tag = _hmac(okm[32:], lockbox[:113])
    diff = 0
    for a, b in zip(tag, lockbox[113:]):
        diff |= a ^ b
    if diff:
        raise ValueError("the lockbox isn't for this camera, or was altered")
    if aes is None:
        from cryptolib import aes
    return aes(okm[:32], 2, lockbox[65:81]).decrypt(lockbox[81:113])

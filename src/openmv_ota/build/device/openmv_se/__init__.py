"""``openmv_se`` (applications use it as ``openmv_ota.se``) -- the camera's keys: private keys
that never leave the camera, so a signature from one proves which camera is talking.

    from openmv_ota import se
    keys = se.open()                  # None on a board without any
    if keys:
        keys.public_key()             # 65 bytes, 04 || X || Y (P-256): the identity key
        keys.certificate()            # the chip maker's DER X.509 certificate for that key,
                                      # or None where there is none
        keys.sign(digest)             # DER ECDSA over a 32-byte SHA-256 digest
        keys.random(32)               # bytes from the hardware RNG
        keys.ecdh_public_key()        # 65 bytes: the exchange key's public half
        keys.ecdh(peer)               # 32 bytes: its ECDH secret with a peer's public key

Two keys, one job each: the identity key signs (who the camera is); the exchange key only does
ECDH (so a server can wrap something only this camera can open). They are made once, at a desk:
:func:`provision`, which ``openmv-ota flash`` (and registration) runs. :func:`open` only reads --
on a camera that was never provisioned, or whose keys are damaged, it raises; a camera in the
field never makes new keys, or changes its identity, on its own.

Where the keys live is board data, not code: a secure element chip (``se050``, ``atecc608``) or,
on a board with none, the camera's own key area at the end of its boot partition (``soft``).
Every module implements the same ``SecureElement`` interface.

FROZEN INTO THE FIRMWARE, not shipped in the romfs: recovery runs exactly when the romfs is gone,
and it signs its check-ins and opens its updates with these keys too. The firmware build freezes
only the board's module and writes its wiring into ``board.py`` here (bus, address, enable pin);
a board with neither freezes none of this package. The romfs's ``openmv_ota.se`` is this, by
another name.

RAM BUDGET: this module runs inside your application, so its memory is your memory. Nothing is
allocated until :func:`open`; a module's buffers are a few hundred bytes.
"""


def _wiring():
    from . import board
    if board.BUS is None:                         # its own keys, not a chip on a bus
        return board, ()
    from machine import I2C, Pin
    enable = Pin(board.ENABLE, Pin.OUT) if board.ENABLE else None
    return board, (I2C(board.BUS, freq=board.FREQ), board.ADDR, enable)


def open():
    """This board's keys, ready to use; None if the board has none. Only reads: raises if the
    camera was never provisioned (``NotProvisioned``), or if its keys are damaged."""
    try:
        board, wiring = _wiring()
    except ImportError:
        return None
    return board.SecureElement(*wiring)


def provision():
    """``(keys, made)``: make whatever this camera's keys still need -- on a blank ATECC608,
    Arduino's configuration and its one-way locks first -- then open them. ``made`` is False if
    it had them all already; nothing that exists is ever replaced. Raises, changing nothing, if
    the keys are damaged. A desk step: ``openmv-ota flash`` and registration call it, an
    application never does. None on a board with no keys."""
    try:
        board, wiring = _wiring()
    except ImportError:
        return None
    return board.SecureElement.provision(*wiring)


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
                _keys = open()
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
            _keys = open()
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

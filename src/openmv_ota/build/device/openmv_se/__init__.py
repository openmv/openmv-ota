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
    blob = se.seal(data, aad=b"records")   # bytes only this camera can open (see below)
    data = se.unseal(blob, aad=b"records") # ...and that fail to open if altered

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

**Sealing data** (:func:`seal`, :func:`unseal`) is what the keys offer an application beyond
identity: a record, a token or a setting that must be kept on the camera but not readable off
it -- on ``/flash``, which any PC can mount -- is sealed to this camera instead of being keyed on
something guessable like its serial number. The seal key is derived from the exchange key, so it
is as well kept as that key is: inside the chip on a board with a secure element, sealed under
the N6's hardware key, and in the key area (readable until lockdown) on the other STM32 boards.

RAM BUDGET: this module runs inside your application, so its memory is your memory. Nothing is
allocated until :func:`open`; a module's buffers are a few hundred bytes. Sealing holds the
caller's data and its blob (the data rounded up to 16 bytes, plus 49) whole, both the caller's
to size; the derived secret is 32 bytes, kept once.
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


def recover(log=None):
    """Power-cycle a secure element that a reset left wedged, on a board that has no enable pin
    to do it with but whose deep sleep cuts the chip's power (wiring ``RECOVER = "deepsleep"``).
    boot.py calls it before anything needs the keys, and before a trial boot spends one of its
    attempts, so the extra boot costs the trial nothing.

    Returns when the chip answers or the board has no such recovery, and without even asking
    the chip on a boot a deep sleep started (``machine.DEEPSLEEP_RESET``): its power was just
    cut, so it cannot still be wedged -- the camera's own deep sleeps cost nothing here, and the
    recovery happens once. Otherwise the camera deep sleeps for 2 s and boots again; that boot
    then reads ``DEEPSLEEP_RESET`` from ``machine.reset_cause()``, so the warning logged before
    the sleep names the reset that wedged the chip. It never sleeps on firmware that cannot tell
    a deep-sleep wake from a reset (no ``machine.DEEPSLEEP_RESET``, or a ``reset_cause()`` of
    0), where sleeping could repeat forever: the camera carries on, and opening its keys says
    why."""
    try:
        board, wiring = _wiring()
    except ImportError:
        return
    if getattr(board, "RECOVER", None) != "deepsleep":
        return
    import machine
    woke = getattr(machine, "DEEPSLEEP_RESET", None)
    cause = machine.reset_cause()
    if woke is not None and cause == woke:
        return
    if board.SecureElement.answers(wiring[0], wiring[1]):
        return
    if woke is None or not cause:
        if log is not None:
            log.warning("se: no answer, and this firmware cannot recover it")
        return
    if log is not None:
        log.warning("se: no answer after reset cause %d -- cutting its power with a 2 s deep "
                    "sleep" % cause)
    import time
    time.sleep(0.02)                              # let the log line out of the UART first
    machine.deepsleep(2000)                       # whole seconds: < 1000 sets no wake alarm


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


# --- sealing data for this camera -----------------------------------------------------------
# The lockbox's construction (``trust.open_lockbox``; cryptolib has no GCM), keyed from the
# camera's own exchange key instead of a peer's:
#
#     secret        = ECDH(exchange key, its own public half)   -- where the key lives, once
#     k_enc || k_mac = HKDF-SHA256(secret, SEAL_INFO || aad)     -- 32 + 32 bytes
#     blob          = SEAL_VERSION || iv (16) || AES-256-CBC(k_enc, iv, pad(data)) || tag (32)
#     tag           = HMAC-SHA256(k_mac, everything before it)
#
# ``aad`` is the purpose (``b"records"``): it goes into the keys, so a blob sealed for one purpose
# does not open under another. The padding is PKCS#7 and is checked only after the tag, so a
# wrong blob is refused before anything is decrypted. ``SEAL_VERSION`` is the format: a later
# construction is a new version, never a reinterpretation of this one.
SEAL_INFO = b"omv-seal-v1\x00"
SEAL_VERSION = 1
_SEAL_OVERHEAD = 1 + 16 + 32                   # version, iv, tag -- plus 1..16 bytes of padding
_secret = None                                 # the exchange key's self-ECDH, derived once


def _hmac(key, msg):
    """HMAC-SHA256 (``key`` at most 64 bytes, which is all this package uses)."""
    import hashlib
    key = key + bytes(64 - len(key))
    inner = hashlib.sha256(bytes(b ^ 0x36 for b in key) + msg).digest()
    return hashlib.sha256(bytes(b ^ 0x5C for b in key) + inner).digest()


def _hkdf64(secret, info):
    """64 bytes of HKDF-SHA256 with no salt (RFC 5869: a salt of 32 zero bytes)."""
    prk = _hmac(bytes(32), secret)
    t1 = _hmac(prk, info + b"\x01")
    return t1 + _hmac(prk, t1 + info + b"\x02")


def _seal_keys(aad, keys):
    """``(k_enc, k_mac)`` for ``aad``, from the camera's seal secret -- the exchange key's ECDH
    with its own public half, computed where the key lives (the chip, or mbedtls over the key
    area) the first time and kept. ValueError on a camera with no keys."""
    global _secret
    if _secret is None:
        if keys is None:
            keys = open()
        if keys is None:
            raise ValueError("this camera has no keys to seal with")
        _secret = bytes(keys.ecdh(keys.ecdh_public_key()))
    okm = _hkdf64(_secret, SEAL_INFO + bytes(aad))
    return okm[:32], okm[32:]


def seal(data, aad=b"", keys=None, aes=None):
    """``data`` sealed for this camera: bytes that only it can open (:func:`unseal`) and that
    fail to open if altered -- for a record on ``/flash`` (host-mountable, so readable and
    editable from a PC), a token, a setting. ``aad`` names the purpose (``b"records"``); a blob
    sealed under one purpose does not open under another. The blob is ``len(data)`` rounded up
    to a multiple of 16, plus 49 bytes.

    Raises ValueError on a camera with no keys (``NotProvisioned`` on one never provisioned), and
    ImportError on a firmware with no ``cryptolib`` -- no TLS, so no AES: the classic boards.
    ``keys``/``aes`` are injectable for tests (``aes`` defaults to ``cryptolib.aes``)."""
    k_enc, k_mac = _seal_keys(aad, keys)
    if aes is None:
        from cryptolib import aes
    import os
    data = bytes(data)
    n = 16 - len(data) % 16
    head = bytes([SEAL_VERSION]) + os.urandom(16)
    body = head + aes(k_enc, 2, head[1:]).encrypt(data + bytes([n]) * n)
    return body + _hmac(k_mac, body)


def unseal(blob, aad=b"", keys=None, aes=None):
    """The data :func:`seal` sealed -- if ``blob`` is this camera's, was sealed under this
    ``aad``, and is unaltered. ValueError otherwise, and never wrong data: the tag is checked
    before anything is decrypted. ``keys``/``aes`` as :func:`seal`."""
    blob = bytes(blob)
    if (len(blob) < _SEAL_OVERHEAD + 16 or (len(blob) - _SEAL_OVERHEAD) % 16
            or blob[0] != SEAL_VERSION):
        raise ValueError("not a sealed blob")
    k_enc, k_mac = _seal_keys(aad, keys)
    body, tag = blob[:-32], blob[-32:]
    diff = 0
    for a, b in zip(_hmac(k_mac, body), tag):
        diff |= a ^ b
    if diff:
        raise ValueError("the blob isn't this camera's, or was altered")
    if aes is None:
        from cryptolib import aes
    plain = aes(k_enc, 2, body[1:17]).decrypt(body[17:])
    n = plain[-1]
    if not 1 <= n <= 16 or plain[-n:] != bytes([n]) * n:
        raise ValueError("the blob isn't this camera's, or was altered")
    return plain[:-n]

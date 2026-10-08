"""``openmv_ota.se`` -- the camera's keys: private keys that never leave the camera, so a
signature from one proves which camera is talking.

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
Every module implements the same ``SecureElement`` interface. The romfs build ships only the
board's module and writes its wiring into ``board.py`` here (bus, address, enable pin); a board
with neither ships none of this package.

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

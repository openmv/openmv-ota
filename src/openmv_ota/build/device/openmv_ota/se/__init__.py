"""``openmv_ota.se`` -- the camera's secure element: a key that never leaves the chip, so a
signature from it proves which camera is talking.

    from openmv_ota import se
    chip = se.open()                  # None on a board without one
    if chip:
        chip.public_key()             # 65 bytes, 04 || X || Y (P-256)
        chip.certificate()            # the chip maker's DER X.509 certificate for that key,
                                      # or None where the chip carries none
        chip.sign(digest)             # DER ECDSA over a 32-byte SHA-256 digest
        chip.random(32)               # bytes from the chip's TRNG
        chip.ecdh_public_key()        # 65 bytes: the second, exchange key's public half
        chip.ecdh(peer)               # 32 bytes: its ECDH secret with a peer's public key

Two keys: the identity key signs (who the camera is); the exchange key only does ECDH (so a
server can wrap something only this camera can open). Every module (``se050``, ``atecc608``,
``soft``) implements that same ``SecureElement`` interface. ``soft`` is a board with no secure
element keeping its own keys, in a key area at the end of its boot partition. Which one a
board has, and how it is wired, is board data, not code: the romfs build ships only that
board's module and writes its wiring into ``board.py`` here (bus, address, enable pin), and a
board with neither ships none of this package.

RAM BUDGET: this module runs inside your application, so its memory is your memory. Nothing is
allocated until :func:`open`; the chip module's buffers are a few hundred bytes.
"""


def open():
    """This board's keys, ready: its secure element powered up, or its own key area opened
    (both made on first open where they need to be); None if the board has neither."""
    try:
        from . import board
    except ImportError:
        return None
    if board.BUS is None:                         # its own keys, not a chip on a bus
        return board.SecureElement()
    from machine import I2C, Pin
    enable = Pin(board.ENABLE, Pin.OUT) if board.ENABLE else None
    return board.SecureElement(I2C(board.BUS, freq=board.FREQ), board.ADDR, enable)

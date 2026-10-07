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

Every chip module (``se050``, ...) implements that same ``SecureElement`` interface. Which one
a board has, and how it is wired, is board data, not code: the romfs build ships only that
board's chip module and writes its wiring into ``board.py`` here (bus, address, enable pin),
and a board with no secure element ships none of this package.

RAM BUDGET: this module runs inside your application, so its memory is your memory. Nothing is
allocated until :func:`open`; the chip module's buffers are a few hundred bytes.
"""


def open():
    """This board's secure element, powered up and ready, or None if the board has none."""
    try:
        from . import board
    except ImportError:
        return None
    from machine import I2C, Pin
    enable = Pin(board.ENABLE, Pin.OUT) if board.ENABLE else None
    return board.SecureElement(I2C(board.BUS, freq=board.FREQ), board.ADDR, enable)

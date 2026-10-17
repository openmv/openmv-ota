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


def recover(log=None):
    """Power-cycle a secure element that a reset left wedged, on a board that has no enable pin
    to do it with but whose deep sleep cuts the chip's power (wiring ``RECOVER = "deepsleep"``).
    boot.py calls it before anything needs the keys, and before a trial boot spends one of its
    attempts, so the extra boot costs the trial nothing.

    Returns when the chip answers or the board has no such recovery. Otherwise the camera deep
    sleeps for 2 s and boots again -- once: if the chip still does not answer on the boot that
    deep sleep started, this returns and the camera carries on without its keys (opening them
    then says why). It also returns without sleeping on firmware that cannot tell a deep-sleep
    wake from a reset (no ``machine.DEEPSLEEP_RESET``, or a ``reset_cause()`` of 0), where
    sleeping could repeat forever."""
    try:
        board, wiring = _wiring()
    except ImportError:
        return
    if getattr(board, "RECOVER", None) != "deepsleep":
        return
    if board.SecureElement.answers(wiring[0], wiring[1]):
        return
    import machine
    woke = getattr(machine, "DEEPSLEEP_RESET", None)
    cause = machine.reset_cause()
    if woke is None or not cause or cause == woke:
        if log is not None:
            log.warning("se: no answer, and %s" % ("a deep sleep did not help" if cause == woke
                                                   else "this firmware cannot recover it"))
        return
    if log is not None:
        log.warning("se: no answer -- cutting its power with a 2 s deep sleep")
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

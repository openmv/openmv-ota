"""The slot cipher, where the chip's engine covers the whole flash part (the AE3).

The N6's and the RT1062's engines decipher one range, set and locked by the boot stub before
the firmware runs: their firmware does not carry this module. The AE3's deciphers every read of
the part's memory map under one key, which it takes from the running firmware (the Secure
Enclave's service). So boot.py calls :func:`start` to key it for the slot the stub booted and
switch it on, before the slot is read; from then on the slot's body reads in the clear and
everything else on the part is read through ``flash_crypt.raw``, which undoes the engine.

The key is the camera's own: sixteen random bytes made here, on the first boot, in the first
blank place of the key area's first 256 bytes (the stub's ecb_slot.h has the layout). A camera
that has just made its key resets: the stub then ciphers the slot it was flashed with.

Frozen beside boot.py, and only into a firmware that keys its engine itself: a board with no
flash to spare does not pay for it.

RAM BUDGET: this module runs inside your application, so its memory is your memory. Every
buffer here has a ceiling. Nothing is sized by a file's length, a response body, a length field
off the wire, or a queue that grows while the network is down: it reads one control block (a
slot's trailer) and 64-byte places of the key area, and nothing else.
"""

import hashlib
import struct

_SLOT_CRYPT_LABEL = b"omv-slot-crypt-1"            # the stub's slot_label
_FLASH_KEY_MAGIC = b"OMVF\x01"
_FLASH_KEY_PLACE = 64                              # bytes per place: the commit word, the key word
_FLASH_KEY_PLACES = 4
_ROMFS_MAGIC = b"\xd2\xcd1"                        # a slot body stored in the clear starts with it


def slot_tweak(index, trailer, parse):
    """The tweak slot ``index`` (0 = A, 1 = B) is ciphered under for the image whose trailer
    block is ``trailer`` (``parse`` is boot.py's ``parse_trailer``); None if it does not
    parse."""
    try:
        t = parse(trailer)
    except Exception:
        return None
    h = hashlib.sha256(_SLOT_CRYPT_LABEL + struct.pack("<I", index)
                       + hashlib.sha256(t.signed_region).digest())
    return h.digest()[:10]


def booted_tweak(ob, parse):
    """The tweak of the slot the stub's record names (``ob`` is boot.py's ``OtaBoot``); None
    with no record, or when that slot is stored in the clear (flashed, and not yet ciphered by
    the stub) or its trailer does not parse."""
    if ob.record is None:
        return None
    index = ob.record[1]
    for i, (_name, offset, size) in enumerate(ob._slots()):
        if i == index:
            if bytes(ob.read(offset, len(_ROMFS_MAGIC))) == _ROMFS_MAGIC:
                return None
            return slot_tweak(index, ob.read(offset + size - ob.block, ob.block), parse)
    return None


def make_flash_key(store, random):
    """Make the camera's flash key in the first blank place of the key area (``store`` is
    ``key_store``): the key word first, the word that commits it last, so a power cut leaves a
    place that never counts and the next call uses the one after it. True if a key was
    written; False if no place is blank."""
    blank = bytes([store.BLANK]) * _FLASH_KEY_PLACE
    for i in range(_FLASH_KEY_PLACES):
        off = i * _FLASH_KEY_PLACE
        if bytes(store.read(off, _FLASH_KEY_PLACE)) != blank:
            continue
        key = bytes(random(16))
        store.write(off + 32, key + bytes(b ^ 0xFF for b in key))
        store.write(off, _FLASH_KEY_MAGIC + blank[:32 - len(_FLASH_KEY_MAGIC)])
        return True
    return False


def start(crypt, ob, store, random, reset, log, parse):
    """Key and switch on the engine for the slot the stub booted (``crypt`` is the AE3's
    ``flash_crypt``; ``ob`` the ``OtaBoot`` about to run). A camera with no flash key makes one
    and resets (``reset`` does not return on a device). Never raises: a camera whose engine
    cannot be started boots on, and the slot pick that follows says what it found."""
    try:
        if crypt.enabled():
            log.debug("boot: slot engine already on")      # a soft reset: it stays keyed
            return
        # A reset that restarted this firmware without resetting the engine (the reset pin)
        # leaves it on under a key nothing here knows: off, before anything is read.
        crypt.disable()
        if not crypt.keyed():
            if make_flash_key(store, random) and crypt.keyed():
                log.info("boot: flash key made, restarting")
                reset()
                return
            log.error("boot: no flash key, and none could be made")
            return
        tweak = booted_tweak(ob, parse)
        if tweak is None:
            log.info("boot: slot engine off (no ciphered slot)")
            return
        crypt.enable(tweak)
        log.info("boot: slot engine on")
    except Exception as e:
        log.error("boot: slot engine failed: %r" % (e,))

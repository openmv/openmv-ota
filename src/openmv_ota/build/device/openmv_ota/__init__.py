"""OpenMV OTA runtime helpers for the *main* core (packed into ``/rom/lib``).

``openmv-ota project new --ota`` scaffolds this package into a project's
``app/lib/openmv_ota/``; it runs under MicroPython on the camera. The public calls
are what an app uses around an OTA update:

    status()   -> a dict describing the running FRONT image's trial state (read-only)
    confirm()  -> keep the running image: write CONFIRMED iff it's an un-confirmed
                  one-shot trial, else no-op (idempotent). Call once your app has
                  validated itself healthy -- NOT blindly at boot, or you defeat the
                  rollback safety.
    sync()     -> apply any bundled resources (``data/resources.json``) whose target
                  partition differs from the bundled copy -- e.g. write the AE3
                  coprocessor romfs into the helper core's partition. Idempotent;
                  call early (before the helper core is used). No-op when there is
                  nothing bundled.

Like the frozen ``boot.py`` this module is self-contained -- it can't import the
host ``openmv_ota.ota.*`` packages under MicroPython, so the status-marker constants
are duplicated here and pinned against the originals by ``test_openmv_ota_runtime``.
The pure logic takes injected I/O so it is host-testable; the device entry points
wire the real ``vfs``/``uctypes``/``_ota_config``.
"""

import hashlib

# --- Status markers (mirror of openmv_ota.ota.status / boot.py) --------------

MARKER_SIZE = 16
_PENDING_OFF = 0
_TRIED_OFF = 16
_CONFIRMED_OFF = 32


def _marker(label):
    return hashlib.sha256(b"openmv-ota.status." + label).digest()[:MARKER_SIZE]


PENDING = _marker(b"pending")
TRIED = _marker(b"tried")
CONFIRMED = _marker(b"confirmed")


def _markers(status):
    """``(pending, tried, confirmed)`` booleans for a status sector."""
    return (status[_PENDING_OFF:_PENDING_OFF + MARKER_SIZE] == PENDING,
            status[_TRIED_OFF:_TRIED_OFF + MARKER_SIZE] == TRIED,
            status[_CONFIRMED_OFF:_CONFIRMED_OFF + MARKER_SIZE] == CONFIRMED)


# --- pure logic (host-testable; all flash I/O injected) ---------------------

def _status_of(status_sector):
    """Decode a FRONT status sector into the app-facing trial state.

    ``trial`` means a one-shot trial that has booted but isn't committed yet
    (pending + tried + not confirmed) -- the state ``confirm()`` acts on."""
    pending, tried, confirmed = _markers(status_sector)
    return {
        "pending": pending,
        "tried": tried,
        "confirmed": confirmed,
        "trial": pending and tried and not confirmed,
    }


def _needs_confirm(status_sector):
    """True iff the FRONT slot is an un-confirmed one-shot trial."""
    return _status_of(status_sector)["trial"]


# Streaming unit for partition compare/write. A multiple of every flash write
# alignment, so chunked writes never need per-port re-alignment, and only one chunk
# is ever held in RAM -- never a whole (up to ~1 MiB) image.
_CHUNK = 4096


def _streams_equal(file_chunks, read_target):
    """True iff a file (yielded as ``file_chunks``) matches a target byte-for-byte.
    ``read_target(off, n)`` returns the ``n`` target bytes at offset ``off``. Streamed:
    one chunk at a time, so neither whole image is materialised in RAM."""
    off = 0
    for chunk in file_chunks:
        if read_target(off, len(chunk)) != chunk:
            return False
        off += len(chunk)
    return True


# --- device entry points ----------------------------------------------------
# Thin wrappers that wire the real vfs/uctypes/_ota_config to the pure logic
# above. Device-only (need MicroPython + a frozen _ota_config), so they're
# excluded from host coverage and exercised under QEMU, exactly like boot.py's
# _main. Flash reads use uctypes.addressof + bytearray_at (not a whole-partition
# memoryview slice), so they're safe past the 16 MiB mark on N6/AE3.

def _front_status_offset(cfg):  # pragma: no cover
    # The FRONT slot's status sector is the block before its trailer block.
    return cfg.FRONT_SIZE - 2 * cfg.OTA_BLOCK


def _read_at(part_index, off, size):  # pragma: no cover
    import uctypes
    import vfs
    base = uctypes.addressof(vfs.rom_ioctl(2, part_index))
    return uctypes.bytearray_at(base + off, size)


def _rom_write(*args):  # pragma: no cover
    """A romfs write ioctl (WRITE_PREPARE / WRITE) that raises on failure: the port
    returns a negative MicroPython errno on error (0 or a positive value on success)."""
    import vfs
    rc = vfs.rom_ioctl(*args)
    if rc < 0:
        raise OSError(-rc)
    return rc


def _file_chunks(path):  # pragma: no cover
    f = open(path, "rb")
    try:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                return
            yield chunk
    finally:
        f.close()


def status():  # pragma: no cover
    """The running FRONT image's trial state: a dict with ``pending`` / ``tried`` /
    ``confirmed`` / ``trial`` (``trial`` == an un-confirmed one-shot trial)."""
    import _ota_config
    sector = _read_at(0, _front_status_offset(_ota_config), 3 * MARKER_SIZE)
    return _status_of(sector)


def confirm():  # pragma: no cover
    """Keep the running FRONT image. Writes CONFIRMED iff it's an un-confirmed
    one-shot trial (no erase -- the marker just programs into the already-erased
    status sector, like boot.py arming ``tried``); a no-op otherwise. Returns True
    iff it just confirmed; raises OSError if the marker write fails. Idempotent --
    safe to call every boot once healthy."""
    import _ota_config
    off = _front_status_offset(_ota_config)
    if not _needs_confirm(_read_at(0, off, 3 * MARKER_SIZE)):
        return False
    _rom_write(4, 0, off + _CONFIRMED_OFF, CONFIRMED)
    return True


def _partition_matches(part_index, path):  # pragma: no cover
    """Stream-compare the file at ``path`` to the start of partition ``part_index``,
    copying neither whole image into RAM (the partition via a uctypes view, the file
    one chunk at a time)."""
    import uctypes
    import vfs
    base = uctypes.addressof(vfs.rom_ioctl(2, part_index))
    return _streams_equal(_file_chunks(path),
                          lambda off, n: uctypes.bytearray_at(base + off, n))


def _apply_partition(part_index, path):  # pragma: no cover
    """Erase + program partition ``part_index`` with the file at ``path``, streamed in
    _CHUNK blocks (never the whole image in RAM). The final block is 0xFF-padded to a
    full chunk -- matching the erased flash, and ignored since the romfs is self-sized."""
    import os
    size = os.stat(path)[6]
    total = (size + _CHUNK - 1) // _CHUNK * _CHUNK
    _rom_write(3, part_index, total)                  # WRITE_PREPARE: erase the region
    off = 0
    for chunk in _file_chunks(path):
        if len(chunk) < _CHUNK:
            chunk = chunk + b"\xff" * (_CHUNK - len(chunk))
        _rom_write(4, part_index, off, chunk)         # WRITE one block
        off += _CHUNK


_HANDLERS = {"partition": _apply_partition}   # resource "handler" -> applier(part, path)


def _data_path(name):  # pragma: no cover
    # __file__ is the package's __init__.py (a full path on MicroPython); the data
    # files sit beside it under data/. (MicroPython's __path__ is a str, not a list,
    # so derive the dir from __file__ instead.)
    return __file__.rsplit("/", 1)[0] + "/data/" + name


def sync():  # pragma: no cover
    """Apply bundled resources (``data/resources.json``) whose target differs from the
    bundled copy -- e.g. write the coprocessor romfs into the helper core's partition.
    Streamed (compare then write) so a multi-MB image is never fully in RAM; idempotent
    (writes only on a difference); a no-op when nothing is bundled. Returns the names
    applied; raises OSError if a write fails. Call early, before the helper core runs."""
    import json
    try:
        manifest = json.load(open(_data_path("resources.json")))
    except OSError:
        return []
    applied = []
    for entry in manifest:
        path = _data_path(entry["file"])
        if _partition_matches(entry["partition"], path):
            continue
        _HANDLERS[entry["handler"]](entry["partition"], path)
        applied.append(entry.get("name", entry["file"]))
    return applied

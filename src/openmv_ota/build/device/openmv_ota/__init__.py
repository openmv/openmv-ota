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


def _resources_to_apply(manifest, current_of):
    """Resources whose bundled bytes differ from what's already on their target.

    ``manifest`` is the decoded ``data/resources.json`` (a list of entries);
    ``current_of(entry)`` returns the bytes currently on the entry's target (or
    ``None`` if unreadable). Returns the entries that need applying, in order."""
    todo = []
    for entry in manifest:
        if bytes(current_of(entry) or b"") != bytes(entry["bytes"]):
            todo.append(entry)
    return todo


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
    iff it just confirmed. Idempotent -- safe to call every boot once healthy."""
    import _ota_config
    import vfs
    off = _front_status_offset(_ota_config)
    if not _needs_confirm(_read_at(0, off, 3 * MARKER_SIZE)):
        return False
    vfs.rom_ioctl(4, 0, off + _CONFIRMED_OFF, CONFIRMED)
    return True


def _apply_partition(part_index, data):  # pragma: no cover
    """Write ``data`` to the start of ROMFS partition ``part_index`` (erase then
    program). The image is self-sized, so any older bytes past it are ignored."""
    import vfs
    vfs.rom_ioctl(3, part_index, len(data))   # WRITE_PREPARE: erase len(data) bytes
    vfs.rom_ioctl(4, part_index, 0, data)     # WRITE at offset 0


_HANDLERS = {"partition": _apply_partition}   # resource "handler" -> applier


def _data_path(name):  # pragma: no cover
    return __path__[0] + "/data/" + name


def sync():  # pragma: no cover
    """Apply bundled resources (``data/resources.json``) whose target differs from
    the bundled copy -- e.g. write the coprocessor romfs into the helper core's
    partition. Idempotent (only writes on a difference); a no-op when nothing is
    bundled. Returns the names applied. Call early, before the helper core is used."""
    import json
    import vfs
    try:
        manifest = json.load(open(_data_path("resources.json")))
    except OSError:
        return []
    for entry in manifest:
        entry["bytes"] = open(_data_path(entry["file"]), "rb").read()

    def current_of(entry):
        whole = bytes(vfs.rom_ioctl(2, entry["partition"]))
        return whole[:len(entry["bytes"])]

    applied = []
    for entry in _resources_to_apply(manifest, current_of):
        _HANDLERS[entry["handler"]](entry["partition"], entry["bytes"])
        applied.append(entry.get("name", entry["file"]))
    return applied

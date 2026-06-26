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
                  coprocessor romfs into the helper core's partition. A flash erase +
                  chunked write of a whole partition, so NOT quick -- it feeds the
                  watchdog (openmv_wdt) like install() does. Idempotent; call early
                  (before the helper core is used). No-op when nothing is bundled.
    install()  -> download a gzipped FRONT-slot image over HTTPS and install it:
                  write the FRONT slot, arm the one-shot trial, reboot. Does NOT
                  return on success. Call with the network already up, after any app
                  teardown (the install erases /rom, so the app can't continue).

Like the frozen ``boot.py`` this module is self-contained -- it can't import the
host ``openmv_ota.ota.*`` packages under MicroPython, so the status-marker constants
are duplicated here and pinned against the originals by ``test_openmv_ota_runtime``.
The pure logic takes injected I/O so it is host-testable; the device entry points
wire the real ``vfs``/``uctypes``/``_ota_config``.
"""

import hashlib

# Re-export the frozen OTA logger so the app can ``openmv_ota.log.info("...")`` (it's the
# standard ``logging.getLogger("openmv_ota")``) and the lib's own device paths can log.
# Absent on the host (and on a firmware built without the frozen openmv_log) -> a null
# logger, so callers never need to guard.
try:
    from openmv_log import log
except ImportError:
    class _NullLog:
        def debug(self, msg, *a):
            pass

        def info(self, msg, *a):
            pass

        def warning(self, msg, *a):
            pass

        def error(self, msg, *a):
            pass

        def critical(self, msg, *a):
            pass

    log = _NullLog()

# The watchdog helper (frozen as openmv_wdt) -- sync() does a flash erase + chunked write
# of a whole partition, which can be slow enough to trip an enabled watchdog, so it feeds
# it the same minimal way install() does. Absent on the host / a build without a watchdog.
try:
    import openmv_wdt as _wdt
except ImportError:
    _wdt = None


class _NoWdt:  # pragma: no cover  (fallback relax() context when no watchdog is frozen)
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _wdt_relax():  # pragma: no cover  (device)
    return _wdt.relax() if _wdt is not None else _NoWdt()


def _wdt_feed():  # pragma: no cover  (device)
    if _wdt is not None:
        _wdt.feed()

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


def _should_confirm(slot, status_sector):
    """True iff confirm() should write CONFIRMED: we actually booted FRONT *and* it's an
    un-confirmed trial. The slot guard matters -- if we fell back to BACK because FRONT's
    trial failed, FRONT still looks like an un-confirmed trial, and confirming it would
    resurrect the bad image on the next boot."""
    return slot == "FRONT" and _needs_confirm(status_sector)


# Streaming unit for partition compare/write. A multiple of every flash write
# alignment, so chunked writes never need per-port re-alignment, and only one chunk
# is ever held in RAM -- never a whole (up to ~1 MiB) image.
_CHUNK = 4096


def _streams_equal(file_chunks, read_target, feed=None):
    """True iff a file (yielded as ``file_chunks``) matches a target byte-for-byte.
    ``read_target(off, n)`` returns the ``n`` target bytes at offset ``off``. Streamed:
    one chunk at a time, so neither whole image is materialised in RAM. ``feed`` (if given)
    is called per chunk -- the already-applied case re-reads the whole partition every
    boot, long enough to need watchdog feeding."""
    off = 0
    for chunk in file_chunks:
        if read_target(off, len(chunk)) != chunk:
            return False
        off += len(chunk)
        if feed is not None:
            feed()
    return True


def _check_readback(actual, expected):
    """Raise OSError if a write/erase read-back differs from what it should be -- the
    extra check that the flash actually took the operation, beyond a success return."""
    if actual != expected:
        raise OSError("flash verify failed")


class _Progress:
    """Throttled progress reporter for ``sync()``'s chunked write: logs at every new 10%
    step -- so a multi-second sync shows movement without a log line per 4 KiB chunk.
    ``label`` is the resource name. (``install()`` can't use this: it erases the partition
    this lib lives in, so it logs from its own RAM-resident reporter in installer.py.)"""

    def __init__(self, label):
        self._label = label
        self._step = -1

    def __call__(self, done, total):
        pct = done * 100 // total if total else 100
        step = pct // 10
        if step > self._step:
            self._step = step
            log.info("%s: %d%% (%d/%d bytes)" % (self._label, pct, done, total))


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


def _write_verified(part_index, off, data):  # pragma: no cover
    """WRITE then read back and verify -- raises OSError on a bad rc or a read-back
    mismatch, so a failed/partial flash write never passes silently."""
    _rom_write(4, part_index, off, data)
    _check_readback(_read_at(part_index, off, len(data)), data)


def _verify_erased(part_index, total):  # pragma: no cover
    """Read back a just-erased region (streamed, a chunk at a time) and raise unless it
    is all 0xFF."""
    off = 0
    while off < total:
        n = _CHUNK if total - off >= _CHUNK else total - off
        _check_readback(_read_at(part_index, off, n), b"\xff" * n)
        off += n
        _wdt_feed()


def _boot_result():  # pragma: no cover
    """What boot.py recorded this boot (it mirrors its result onto _ota_config):
    ``(slot, payload_version, fallback_reason)``. Defaults if boot.py didn't run."""
    import _ota_config
    return (getattr(_ota_config, "last_slot", None),
            getattr(_ota_config, "last_payload_version", 0),
            getattr(_ota_config, "last_failure_reason", None))


def status():  # pragma: no cover
    """What boot.py did this boot, for the app/updater to inspect or report:

        slot             'FRONT' | 'BACK' | None    which image booted
        fallback_reason  str | None                 why FRONT was rejected (None on FRONT)
        payload_version  int                         the booted image's version
        pending/tried/confirmed/trial               FRONT's trial-marker state

    ``slot == 'BACK'`` with a ``fallback_reason`` means the last update failed and the
    device is on the golden image -- worth reporting upstream."""
    import _ota_config
    slot, version, reason = _boot_result()
    s = _status_of(_read_at(0, _front_status_offset(_ota_config), 3 * MARKER_SIZE))
    s["slot"] = slot
    s["fallback_reason"] = reason
    s["payload_version"] = version
    return s


def identity():  # pragma: no cover
    """The running image's identity/provenance from ``/rom/system.json`` (board, product,
    board_id, app_version, vendor, toolchain, ...) -- what an update server reads to decide
    what to push. ``{}`` if there's no system.json."""
    import json
    try:
        return json.load(open("/rom/system.json"))
    except OSError:
        return {}


def confirm():  # pragma: no cover
    """Keep the running FRONT image. Writes CONFIRMED iff we booted FRONT *and* it's an
    un-confirmed one-shot trial (no erase -- the marker just programs into the
    already-erased status sector, like boot.py arming ``tried``); a no-op otherwise. The
    FRONT-slot guard prevents confirming a failed trial we fell back from. Returns True
    iff it just confirmed; raises OSError if the marker write fails. Idempotent -- safe
    to call every boot once healthy."""
    import _ota_config
    slot, _v, _r = _boot_result()
    off = _front_status_offset(_ota_config)
    if not _should_confirm(slot, _read_at(0, off, 3 * MARKER_SIZE)):
        return False
    _write_verified(0, off + _CONFIRMED_OFF, CONFIRMED)
    log.info("confirm: kept running FRONT image")
    return True


# A resource handler is a ``(matches, apply)`` pair, both taking ``(entry, path)`` --
# ``entry`` is the resources.json record (so each handler reads its own args) and
# ``path`` is the bundled data file. ``matches`` is the idempotence check ("already
# applied?"); ``apply`` does the write. sync() is handler-agnostic, so a future
# resource kind (keys, fuses, ...) is just another entry in _HANDLERS -- no partition
# assumptions baked into the loop.

def _partition_matches(entry, path):  # pragma: no cover
    """matches() for the ``partition`` handler: stream-compare the file to the start of
    partition ``entry["partition"]`` (the partition via a uctypes view, the file one
    chunk at a time -- neither whole image in RAM)."""
    import uctypes
    import vfs
    base = uctypes.addressof(vfs.rom_ioctl(2, entry["partition"]))
    return _streams_equal(_file_chunks(path),
                          lambda off, n: uctypes.bytearray_at(base + off, n), _wdt_feed)


def _partition_apply(entry, path, progress=None):  # pragma: no cover
    """apply() for the ``partition`` handler: erase + program partition
    ``entry["partition"]`` with the file, streamed in _CHUNK blocks (never the whole
    image in RAM). The final block is 0xFF-padded to a full chunk -- matching the erased
    flash, and ignored since the romfs is self-sized. ``progress(done, total)`` (if given)
    reports the write's advance per chunk."""
    import os
    part_index = entry["partition"]
    size = os.stat(path)[6]
    total = (size + _CHUNK - 1) // _CHUNK * _CHUNK
    with _wdt_relax():                                 # the erase is the one op we can't
        _rom_write(3, part_index, total)              # feed from a loop (WRITE_PREPARE)
    _verify_erased(part_index, total)                 # read back -> confirm all 0xFF (feeds)
    off = 0
    for chunk in _file_chunks(path):
        if len(chunk) < _CHUNK:
            chunk = chunk + b"\xff" * (_CHUNK - len(chunk))
        _write_verified(part_index, off, chunk)       # WRITE one block + verify
        off += _CHUNK
        _wdt_feed()                                   # per chunk, like the installer
        if progress is not None:
            progress(off if off < total else total, total)


# resource kind -> (matches, apply); add new kinds here without touching sync().
_HANDLERS = {"partition": (_partition_matches, _partition_apply)}


def _data_path(name):  # pragma: no cover
    # __file__ is the package's __init__.py (a full path on MicroPython); the data
    # files sit beside it under data/. (MicroPython's __path__ is a str, not a list,
    # so derive the dir from __file__ instead.)
    return __file__.rsplit("/", 1)[0] + "/data/" + name


def sync():  # pragma: no cover
    """Apply bundled resources (``data/resources.json``) whose target differs from the
    bundled copy -- today the coprocessor romfs into the helper core's partition, but the
    loop is handler-agnostic (a resource's ``handler`` selects a (matches, apply) pair,
    so future kinds like keys/fuses just add a handler). Streamed (compare then write) so
    a multi-MB image is never fully in RAM; idempotent (applies only on a difference); a
    no-op when nothing is bundled. A flash erase + chunked write of a whole partition, so
    NOT quick -- it feeds the watchdog (openmv_wdt) the same minimal way install() does
    (relax() around the erase, feed() per chunk, including the already-applied re-read).
    Each resource's write is logged at every 10% step. Returns the names applied; raises
    OSError if a write fails. Call early, before the helper core runs."""
    import json
    try:
        manifest = json.load(open(_data_path("resources.json")))
    except OSError:
        return []
    applied = []
    for entry in manifest:
        path = _data_path(entry["file"])
        name = entry.get("name", entry["file"])
        matches, apply = _HANDLERS[entry["handler"]]
        if matches(entry, path):
            continue
        log.info("sync: applying " + name)
        apply(entry, path, _Progress("sync " + name))
        applied.append(name)
    if applied:
        log.info("sync: applied resource(s): " + ", ".join(applied))
    return applied


def install(url, ca=None):  # pragma: no cover
    """Download a gzipped FRONT-slot OTA image over HTTPS and install it: write the new
    image into the FRONT slot, arm the one-shot trial, and reboot into it.

    Does **not** return on success -- it reboots. A failure *after* the write commits
    reboots into the golden BACK image instead (boot.py rejects the half-written FRONT);
    a pre-flight failure (bad URL, DNS, TLS, HTTP status) raises before anything is
    erased, so the app can catch it and retry without a reboot. Call once the network is
    up (WiFi/Ethernet/HaLow) and after any app teardown -- the install erases ``/rom``,
    so the running app cannot continue past this call.

    The heavy lifting lives in ``data/installer.py``, shipped as source and ``exec``'d
    into RAM here: the app's code is in the FRONT slot we're about to erase, so the
    installer must run from RAM, not XIP from that slot. For that same reason install
    progress is *logged* by the installer (RAM + the frozen logger), not delivered to a
    caller callback -- any callback here (this lib, the app) lives in the slot being
    erased, so calling it post-erase would XIP from erased flash. (``sync()`` *does* take
    an ``on_progress`` -- it erases a different partition, leaving this one intact.) ``ca``
    are the TLS trust anchors (PEM): ``None`` uses the bundled ``data/ca.pem`` (the Mozilla
    root bundle), ``bytes`` are used as-is, and a ``str`` is a path to read."""
    import _ota_config as cfg
    here = __file__.rsplit("/", 1)[0]
    if ca is None:
        ca = _read_file(here + "/data/ca.pem", "rb")
    elif isinstance(ca, str):
        ca = _read_file(ca, "rb")
    ns = {}
    exec(_read_file(here + "/data/installer.py", "r"), ns)
    ns["run"](url, ca, cfg)


def _read_file(path, mode):  # pragma: no cover
    f = open(path, mode)
    try:
        return f.read()
    finally:
        f.close()

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

RAM BUDGET: this module runs inside your application, so its memory is your
memory. Every buffer here has a ceiling. Nothing is sized by a file's length, a
response body, a length field off the wire, or a queue that grows while the
network is down: reads use bounded windows of a few KB, anything larger is
streamed, and large data is aliased with memoryview/bytearray_at rather than
copied.
"""

import hashlib
import struct

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
_REPR_OFF = 48
_STATUS_READ = 4 * MARKER_SIZE                   # pending/tried/confirmed + repr


def _marker(label):
    return hashlib.sha256(b"openmv-ota.status." + label).digest()[:MARKER_SIZE]


PENDING = _marker(b"pending")
TRIED = _marker(b"tried")
CONFIRMED = _marker(b"confirmed")
REPR_FULL = _marker(b"repr.full")
REPR_DELTA = _marker(b"repr.ocdl")


def _markers(status):
    """``(pending, tried, confirmed)`` booleans for a status sector."""
    return (status[_PENDING_OFF:_PENDING_OFF + MARKER_SIZE] == PENDING,
            status[_TRIED_OFF:_TRIED_OFF + MARKER_SIZE] == TRIED,
            status[_CONFIRMED_OFF:_CONFIRMED_OFF + MARKER_SIZE] == CONFIRMED)


def _representation_of(status):
    """How the FRONT image was installed: ``"full"`` / ``"delta"`` / ``None`` (unwritten)."""
    m = status[_REPR_OFF:_REPR_OFF + MARKER_SIZE]
    if m == REPR_FULL:
        return "full"
    if m == REPR_DELTA:
        return "delta"
    return None


# --- Anti-rollback floor (mirror of openmv_ota.ota.rollback) -----------------

_ROLLBACK_ENTRY = 8                              # u32 version || u32 ~version


def _rollback_entry(version):
    return struct.pack("<II", version & 0xFFFFFFFF, (version & 0xFFFFFFFF) ^ 0xFFFFFFFF)


def _rollback_floor_of(sector):
    """The highest valid version recorded in a rollback sector (0 if none)."""
    floor = i = 0
    n = len(sector)
    while i + _ROLLBACK_ENTRY <= n:
        version, check = struct.unpack_from("<II", sector, i)
        if (version ^ 0xFFFFFFFF) == check and version > floor:
            floor = version
        i += _ROLLBACK_ENTRY
    return floor


def _rollback_append_offset(sector):
    """Offset of the first blank entry slot, or None if the sector is full."""
    blank = b"\xff" * _ROLLBACK_ENTRY
    i = 0
    n = len(sector)
    while i + _ROLLBACK_ENTRY <= n:
        if bytes(sector[i:i + _ROLLBACK_ENTRY]) == blank:
            return i
        i += _ROLLBACK_ENTRY
    return None


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
_RESP_MAX = 8 * 1024         # a check-in reply is grants + version info;
                             # kept roomy on purpose -- rejecting a real
                             # reply breaks OTA, the costlier failure
_ASSET_MAX = 256 * 1024      # our own shipped installer.py / ca.pem


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
        representation   'full' | 'delta' | None     how the FRONT image was installed
        pending/tried/confirmed/trial               FRONT's trial-marker state

    ``slot == 'BACK'`` with a ``fallback_reason`` means the last update failed and the
    device is on the golden image -- worth reporting upstream."""
    import _ota_config
    slot, version, reason = _boot_result()
    sector = _read_at(0, _front_status_offset(_ota_config), _STATUS_READ)
    s = _status_of(sector)
    s["slot"] = slot
    s["fallback_reason"] = reason
    s["payload_version"] = version
    s["representation"] = _representation_of(sector)
    return s


def identity():  # pragma: no cover
    """The running image's identity/provenance from ``/rom/system.json`` (board, product,
    product_id, app_version, vendor, toolchain, ...) plus ``device_id`` -- this unit's unique
    hardware id (``machine.unique_id()``) -- so an update server can address the specific
    device, not just the model. ``{}`` (minus device_id) if there's no system.json."""
    import json
    try:
        info = json.load(open("/rom/system.json"))
    except OSError:
        info = {}
    try:
        import machine
        info["device_id"] = machine.unique_id().hex()
    except (ImportError, AttributeError):
        pass
    return info


# --- the check-in loop + the openmv_cloud extension seam --------------------
# run() polls the update server. openmv_cloud (csi/logs) needs to (a) add fields
# to the check-in -- e.g. its live stream names -- and (b) receive the response
# (the live + ingest grants). It registers here on import; the updater NEVER
# imports openmv_cloud, so a pure-OTA device (no cloud SDK) just does OTA.

_checkin_contributors = {}
_checkin_observers = {}


def register_checkin(contribute=None, on_response=None, key=None):
    """The openmv_cloud extension seam. ``contribute() -> dict`` is merged into
    the check-in body each poll; ``on_response(resp)`` is called with each
    check-in response. Both optional; both isolated (a raising extension can't
    break the OTA loop).

    ``key`` makes registration IDEMPOTENT: re-registering with the same key
    REPLACES the prior handlers, so a module re-imported or reloaded never
    double-registers. Omit ``key`` for an independent (always-added)
    registration."""
    ident = key if key is not None else object()
    if contribute is not None:
        _checkin_contributors[ident] = contribute
    if on_response is not None:
        _checkin_observers[ident] = on_response


def _checkin_body(info, st):
    """The base check-in payload from identity() + status() -- pure, so it's
    host-testable; extension fields (e.g. streams) are merged by contributors."""
    return {
        "device_id": info.get("device_id", ""),
        "product_id": int(info.get("product_id", 0) or 0),
        "account_id": info.get("account_id", ""),
        "board": info.get("board"),
        "product": info.get("product"),
        "app_version": info.get("app_version"),
        "payload_version": int(st.get("payload_version", 0) or 0),
        "slot": st.get("slot"),
        "representation": st.get("representation"),
        "fallback_reason": st.get("fallback_reason"),
        "confirmed": bool(st.get("confirmed", False)),
    }


def _collect_body(info, st):
    body = _checkin_body(info, st)
    for contribute in list(_checkin_contributors.values()):
        try:
            extra = contribute()
        except Exception:
            continue                                 # a broken extension is skipped
        if extra:
            body.update(extra)
    return body


def _notify(resp):
    for on_response in list(_checkin_observers.values()):
        try:
            on_response(resp)
        except Exception:
            pass                                     # never break the loop


def _offer(resp):
    """The manifest URL to install, or None -- pure."""
    return resp.get("manifest_url") if resp.get("update") else None


async def run(server_url, self_test=None, wdt=None, poll_after_s=3600,
              ca=None, ntp_host=None):  # pragma: no cover  (device: the network loop)
    """The OTA lifecycle loop (async, so it coexists with the app's asyncio work
    and openmv_cloud's background tasks). On boot it confirms a healthy trial
    (via ``self_test``, or unconditionally if none), then forever: resolve the
    clock, poll the update server, hand the response to registered extensions
    (the live + ingest grants flow to openmv_cloud here), install any offered
    update, and back off. Never returns.

    ``ca`` are TLS anchors (PEM/path); ``None`` uses the bundled ``data/ca.pem``.
    ``ntp_host`` overrides the NTP server used to set the clock when the RTC is
    not already trustworthy (``None`` = ntptime's default pool)."""
    import asyncio
    boot = status()
    if boot.get("trial") and (self_test is None or self_test()):
        confirm()
    if ca is None:
        here = __file__.rsplit("/", 1)[0]
        ca = _read_file(here + "/data/ca.pem", "rb")
    elif isinstance(ca, str):
        ca = _read_file(ca, "rb")
    while True:
        wait = poll_after_s
        _resolve_clock(ntp_host)          # cheap once trusted; retries NTP until network is up
        try:
            resp = await _checkin(server_url, _collect_body(identity(), status()), ca)
            _notify(resp)
            wait = resp.get("poll_after_s", poll_after_s)
            manifest_url = _offer(resp)
            if manifest_url:
                install(manifest_url, ca)            # does not return on success
        except Exception:
            pass                                     # transient failure -> retry next poll
        _wdt_feed()
        await asyncio.sleep(wait)


def _resolve_clock(ntp_host):  # pragma: no cover  (device: RTC + network)
    """Establish a trustworthy wall clock so records can carry real timestamps.
    A no-op once the clock is good (the deep-sleep / coin-cell case resolves on
    the first pass with no network); otherwise it retries NTP each poll until the
    network is up. Defensive: a missing clock module or a failed sync just leaves
    timestamps absent -- ``seq`` still orders every record."""
    try:
        import openmv_rtc
        openmv_rtc.resolve(ntp_host)
    except Exception:
        pass


async def _read_capped(reader, limit):  # pragma: no cover  (device network)
    """Read a response body to EOF, capped at ``limit``. Never ``read(-1)``: a
    captive portal or broken proxy must not get to size our allocation. Bounded
    chunks joined once (no quadratic ``+=``)."""
    chunks, total = [], 0
    while True:
        d = await reader.read(_CHUNK)
        if not d:
            return b"".join(chunks)
        total += len(d)
        if total > limit:
            raise OSError("check-in response over %d bytes" % limit)
        chunks.append(d)


async def _checkin(server_url, body, ca):  # pragma: no cover  (device network)
    """POST the check-in body to ``/api/v1/check`` and return the parsed JSON."""
    import asyncio
    import json
    import ssl
    scheme, _, rest = server_url.rstrip("/").partition("://")
    hostport, _, _ = rest.partition("/")
    host, _, port = hostport.partition(":")
    port = int(port) if port else (443 if scheme == "https" else 80)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cadata=ca.decode() if isinstance(ca, bytes) else ca)
    reader, writer = await asyncio.open_connection(host, port, ssl=ctx)
    try:
        payload = json.dumps(body).encode()
        writer.write((
            "POST /api/v1/check HTTP/1.1\r\nHost: %s\r\nUser-Agent: openmv-cam/1.0\r\n"
            "Content-Type: application/json\r\nContent-Length: %d\r\n"
            "Connection: close\r\n\r\n" % (host, len(payload))).encode() + payload)
        await writer.drain()
        status_line = await reader.readline()
        if b" 200 " not in status_line and not status_line.rstrip().endswith(b" 200"):
            raise OSError("check-in HTTP %s" % status_line)
        while True:                                  # skip headers
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
        return json.loads(await _read_capped(reader, _RESP_MAX))
    finally:
        writer.close()
        await writer.wait_closed()


def _advance_rollback(cfg, version):  # pragma: no cover (device)
    """Raise the anti-rollback floor to ``version`` by appending it to BACK's rollback
    sector (a 1->0 program, no erase). A no-op if the floor already covers ``version`` or
    the log is full (the floor then stays frozen at its max -- still protective)."""
    import uctypes
    import vfs
    base = uctypes.addressof(vfs.rom_ioctl(2, 0))
    off = cfg.PARTITION_SIZE - 3 * cfg.OTA_BLOCK     # BACK's rollback sector (absolute)
    sector = uctypes.bytearray_at(base + off, cfg.OTA_BLOCK)
    if _rollback_floor_of(sector) >= version:
        return
    pos = _rollback_append_offset(sector)
    if pos is None:
        return
    _write_verified(0, off + pos, _rollback_entry(version))


def confirm():  # pragma: no cover
    """Keep the running FRONT image: raise the anti-rollback floor to this version, then
    write CONFIRMED -- iff we booted FRONT *and* it's an un-confirmed one-shot trial (a
    no-op otherwise). Advancing the floor *before* CONFIRMED means a crash in between leaves
    the floor raised but the image un-confirmed, so the next boot safely falls back to the
    golden (which the floor never locks out). The FRONT-slot guard prevents confirming a
    failed trial we fell back from. Returns True iff it just confirmed; raises OSError if a
    write fails. Idempotent -- safe to call every boot once healthy."""
    import _ota_config
    slot, version, _r = _boot_result()
    off = _front_status_offset(_ota_config)
    if not _should_confirm(slot, _read_at(0, off, 3 * MARKER_SIZE)):
        return False
    _advance_rollback(_ota_config, version)
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
    """Fetch the signed update **manifest** at ``url`` and install the image it points to:
    verify the manifest's signature (same trusted keys as the image trailer), check the
    device-relative fields (board / platform / anti-rollback) and pick a representation,
    then download that image into the FRONT slot, arm the one-shot trial, and reboot.

    ``url`` is the **manifest** URL (produced by ``build ota-romfs``), not a raw image --
    the device resolves the actual image URL from the signed manifest internally. Does
    **not** return on success -- it reboots. A failure *after* the write commits reboots
    into the golden BACK image instead (boot.py rejects the half-written FRONT); a
    pre-flight failure (bad URL, DNS, TLS, a bad/forbidden/rolled-back manifest) raises
    before anything is erased, so the app can catch it and retry without a reboot. Call
    once the network is up (WiFi/Ethernet/HaLow) and after any app teardown -- the install
    erases ``/rom``, so the running app cannot continue past this call.

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


def _read_file(path, mode, limit=_ASSET_MAX):  # pragma: no cover
    """Read one of our OWN shipped assets (installer.py, ca.pem) whole -- they
    have to be whole to exec()/parse. Still bounded: these are fixed build
    artifacts, so exceeding the ceiling means a corrupt romfs, not a big input."""
    f = open(path, mode)
    try:
        data = f.read(limit + 1)
        if len(data) > limit:
            raise OSError("%s exceeds the %d-byte asset ceiling" % (path, limit))
        return data
    finally:
        f.close()

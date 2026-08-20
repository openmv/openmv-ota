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
        return self  # hil-residual: bare return of self (CM enter)

    def __exit__(self, *a):
        return False  # hil-residual: bare const return (CM exit)


def _wdt_relax():  # pragma: no cover  (device)  # hil-residual-fn: thin wrapper over openmv_wdt.relax; callers (run() check-in, _partition_apply) are device-network/coproc paths, exercised only under an ENABLED watchdog
    return _wdt.relax() if _wdt is not None else _NoWdt()


def _wdt_feed():  # pragma: no cover  (device)
    if _wdt is not None:
        _wdt.feed()
        log.debug("wdt: feed")                        # HIL path witness (fed each poll in run())

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


_COUNTER_OFF = 64                                # install counter: u32 || ~u32
_COUNTER_LEN = 8
_SLOT_READ = _COUNTER_OFF + _COUNTER_LEN         # markers + repr + counter

# Just enough of a slot trailer to read its payload_version -- the ONLY field wanted here.
# Parsing no further is deliberate: this runs inside the app on every check-in, and the
# authoritative parse (with the signature check that makes the fields trustworthy) already
# happened in boot.py. What is read here is a REPORT, never a decision.
_TRAILER_MAGIC = b"OMVR"
_TRAILER_VERSION_OFF = 32                        # payload_version (pinned by a test)
_TRAILER_READ = _TRAILER_VERSION_OFF + 4


def _trailer_version(trailer):
    """A slot trailer's ``payload_version``; 0 if it does not parse (blank or torn)."""
    if len(trailer) < _TRAILER_READ or bytes(trailer[:4]) != _TRAILER_MAGIC:
        return 0
    return struct.unpack_from("<I", trailer, _TRAILER_VERSION_OFF)[0]


def _install_counter(status):
    """A slot's install counter, or ``None`` if blank/torn (mirror of ``boot.install_counter``)."""
    if len(status) < _COUNTER_OFF + _COUNTER_LEN:
        return None
    value, check = struct.unpack_from("<II", status, _COUNTER_OFF)
    return value if (value ^ 0xFFFFFFFF) == check else None


def _representation_of(status):
    """How this slot's image was installed: ``"full"`` / ``"delta"`` / ``None`` (unwritten)."""
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
    """Decode a slot's status sector into the app-facing trial state.

    ``trial`` means an installed image that has not been committed yet -- the state
    ``confirm()`` acts on. Under v2 that is ``pending and not confirmed``: boot.py no longer
    writes TRIED, because a trial now gets several attempts and each is recorded by consuming
    one byte of the attempt region. TRIED is still decoded (a v1-era slot may carry it) but it
    is no longer part of the decision, and requiring it here would mean NO trial was ever
    confirmable -- every update would roll back."""
    pending, tried, confirmed = _markers(status_sector)
    return {
        "pending": pending,
        "tried": tried,
        "confirmed": confirmed,
        "trial": pending and not confirmed,
    }


def _needs_confirm(status_sector):
    """True iff this slot holds an un-confirmed trial."""
    return _status_of(status_sector)["trial"]


def _should_confirm(slot, status_sector):
    """True iff confirm() should write CONFIRMED: we booted a slot *and* that slot holds an
    un-confirmed trial.

    In v1 this also checked ``slot == 'FRONT'``, because the status sector was always FRONT's:
    if we had fallen back to BACK, FRONT still looked like an un-confirmed trial and confirming
    it would resurrect the bad image. Under A/B the caller reads the RUNNING slot's own sector,
    so the guard is structural rather than a name comparison -- there is no way to confirm a
    slot we did not boot. What remains is the case where boot.py did not run at all
    (``slot`` is None), where there is nothing to confirm."""
    return slot is not None and _needs_confirm(status_sector)


# Streaming unit for partition compare/write. A multiple of every flash write
# alignment, so chunked writes never need per-port re-alignment, and only one chunk
# is ever held in RAM -- never a whole (up to ~1 MiB) image.
_CHUNK = 4096
_RESP_HEADERS_MAX = 64     # most a sane server sends is a handful; this only has to be far enough
#                              above normal that it never trips on a real reply while still ending a
#                              header stream that never does.
_RESP_MAX = 8 * 1024         # a check-in reply is grants + version info;
                             # kept roomy on purpose -- rejecting a real
                             # reply breaks OTA, the costlier failure
_ASSET_MAX = 256 * 1024      # our own shipped installer.py / ca.pem
_CHECKIN_TIMEOUT = 15        # socket timeout (s) for the check-in: bounds the TLS handshake and each
                             # recv so a stalled server can't hang the poll loop forever. settimeout
                             # (not poll) is also what makes the check-in work over the WINC1500 (the
                             # H7 wifi shield), which implements no select/poll -- see _checkin.


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

def _slot_bounds(cfg, slot):
    """``(offset, size)`` of ``slot`` in the partition -- the mirror of ``boot.OtaBoot._slots``
    (pinned by a test). ``None``/unknown slot answers for A, which is where a device that never
    ran boot.py would be looking anyway."""
    front = cfg.FRONT_SIZE
    if front <= 0 or front >= cfg.PARTITION_SIZE:
        return 0, cfg.PARTITION_SIZE                  # SINGLE: one slot, whole partition
    if slot == "B":
        return front, front
    return 0, front


def _status_offset(cfg, slot):
    """Absolute offset of ``slot``'s status sector: the block before its trailer block.

    v1 had this hardcoded to FRONT because FRONT was the only slot anything ever wrote. Under
    A/B, confirm() and status() must read the slot the device is actually RUNNING -- reading
    A's sector while running B would report the wrong trial state and, worse, confirm the
    wrong image."""
    off, size = _slot_bounds(cfg, slot)
    return off + size - 2 * cfg.CONTROL_BLOCK


def _read_at(part_index, off, size):  # pragma: no cover
    import uctypes
    import vfs
    base = uctypes.addressof(vfs.rom_ioctl(2, part_index))
    view = uctypes.bytearray_at(base + off, size)     # the XIP alias -- witnessed below
    log.debug("read: slot alias")                     # HIL path witness (status/verify read each boot)
    return view  # hil-residual: bare return of the aliased view


def _rom_write(*args):  # pragma: no cover
    """A romfs write ioctl (WRITE_PREPARE / WRITE) that raises on failure: the port
    returns a negative MicroPython errno on error (0 or a positive value on success)."""
    import vfs
    rc = vfs.rom_ioctl(*args)
    if rc < 0:
        raise OSError(-rc)  # hil-residual: bare raise on a negative errno (write-fault, inject-only)
    log.debug("write: rom ioctl")                     # HIL path witness (XIP confirm/rollback write)
    return rc  # hil-residual: bare return of the ioctl rc


def _file_chunks(path):  # pragma: no cover  # hil-residual-fn: coprocessor partition path; AE3 HW-blocked (no working HIL coproc rig)
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
    mismatch, so a failed/partial flash write never passes silently.

    Handles BOTH romfs write models, the same split boot.py and the installer make:
      * XIP/ioctl ports (stm32/alif/samd): the ranged ``rom_ioctl(4)`` WRITE, read back
        through the XIP mapping.
      * block-device ports (mimxrt): ``rom_ioctl(4)`` is -EINVAL, so program byte-granularly
        through the block device (3-arg ``writeblocks``, no erase -- markers/rollback entries
        are 1->0 programs into an already-erased region) and read back through the SAME
        device's ``readblocks`` (an immediate write-then-read is coherent -- what the
        installer's per-chunk verify relies on; the XIP mapping can lag a fresh write).
    Without the block-device branch, confirm() crashed with OSError(EINVAL) on mimxrt -- the
    trial installed + booted but could never confirm, so it always rolled back to golden."""
    import vfs
    part = vfs.rom_ioctl(2, part_index)
    if hasattr(part, "ioctl"):                        # block-device (mimxrt)
        bs = part.ioctl(5, 0)                         # MP_BLOCKDEV_IOCTL_BLOCK_SIZE
        part.writeblocks(off // bs, data, off % bs)   # 3-arg: byte-granular, no erase
        back = bytearray(len(data))                   # len(data) is a marker/entry: bounded
        part.readblocks(off // bs, back, off % bs)
        _check_readback(back, data)
        log.debug("verify: write block-device")        # the confirm/rollback write path witness
    else:                                             # XIP/ioctl ports (stm32/alif/samd)
        _rom_write(4, part_index, off, data)
        _check_readback(_read_at(part_index, off, len(data)), data)
        log.debug("verify: write XIP")                 # the confirm/rollback write path witness


def _boot_result():  # pragma: no cover
    """What boot.py recorded this boot (it mirrors its result onto _ota_config):
    ``(slot, payload_version, fallback_reason)``. Defaults if boot.py didn't run."""
    import _ota_config
    result = (getattr(_ota_config, "last_slot", None),
              getattr(_ota_config, "last_payload_version", 0),
              getattr(_ota_config, "last_failure_reason", None))
    log.debug("status: boot result")                  # HIL path witness (boot-result tuple built)
    return result  # hil-residual: bare return of the boot-result tuple


def status():  # pragma: no cover
    """What boot.py did this boot, for the app/updater to inspect or report:

        slot             'A' | 'B' | None            which slot booted
        fallback_reason  str | None                  why the other slot was rejected, if it was
        payload_version  int                         the booted image's version
        representation   'full' | 'delta' | None     how this image was installed
        pending/tried/confirmed/trial                the RUNNING slot's trial-marker state

    A ``fallback_reason`` means the newest slot was rejected and the device is running the
    previous image -- worth reporting upstream. Under A/B that previous image is the last
    update that worked, not a years-old factory build."""
    import _ota_config
    slot, version, reason = _boot_result()
    sector = _read_at(0, _status_offset(_ota_config, slot), _STATUS_READ)
    s = _status_of(sector)
    s["slot"] = slot
    s["fallback_reason"] = reason
    s["payload_version"] = version
    s["representation"] = _representation_of(sector)
    log.debug("status: read")                         # HIL path witness (runs every boot/checkin)
    return s  # hil-residual: bare return of the status dict


def _slot_names(cfg):
    """``['A']`` in single-image mode, ``['A', 'B']`` under A/B -- the mirror of the slot
    table boot.py and the installer build (pinned by a test)."""
    front = cfg.FRONT_SIZE
    if front <= 0 or front >= cfg.PARTITION_SIZE:
        return ["A"]
    return ["A", "B"]


def _slot_report(name, running, sector, version):
    """One slot's line in the check-in payload -- pure, so it is host-testable."""
    pending, _tried, confirmed = _markers(sector)
    return {
        "slot": name,
        "running": name == running,
        "payload_version": int(version),
        "counter": _install_counter(sector),
        "confirmed": bool(confirmed),
        "pending": bool(pending),
    }


def slots():  # pragma: no cover
    """Every slot's state, newest-first by install counter -- what the device is running AND
    what it would fall back to.

    This is the one thing an operator cannot infer from the running image alone, and under A/B
    it is the thing worth knowing: a device running an unconfirmed trial with a confirmed
    previous release behind it is in a different position from one whose only other slot is
    blank. Bounded by construction -- at most two slots, six small fields each -- and the flash
    reads are ``uctypes`` aliases over the XIP mapping, so nothing here copies a sector."""
    import _ota_config
    import uctypes
    import vfs
    base = uctypes.addressof(vfs.rom_ioctl(2, 0))
    running, _v, _r = _boot_result()
    out = []
    for name in _slot_names(_ota_config):
        off, size = _slot_bounds(_ota_config, name)
        sector = uctypes.bytearray_at(base + off + size - 2 * _ota_config.CONTROL_BLOCK,
                                      _SLOT_READ)
        trailer = uctypes.bytearray_at(base + off + size - _ota_config.CONTROL_BLOCK,
                                       _TRAILER_READ)
        out.append(_slot_report(name, running, sector, _trailer_version(trailer)))
        log.debug("status: slot read")                 # bounded: once per slot (at most twice)
    out.sort(key=_counter_key, reverse=True)
    log.debug("status: slots ready")                   # ...and once for the sorted result
    return out  # hil-residual: bare return of the slot list


def _counter_key(entry):
    """Sort key for :func:`slots`: an unreadable counter sorts LAST, exactly as it does in
    ``boot.select_slot`` -- a slot we cannot order is never claimed to be the newest."""
    counter = entry.get("counter")
    return -1 if counter is None else counter


def identity():  # pragma: no cover
    """The running image's identity/provenance from ``/rom/system.json`` (board, product,
    product_id, app_version, vendor, toolchain, ...) plus ``device_id`` -- this unit's unique
    hardware id (``machine.unique_id()``) -- so an update server can address the specific
    device, not just the model. ``{}`` (minus device_id) if there's no system.json."""
    import json
    try:
        info = json.load(open("/rom/system.json"))
    except OSError:  # hil-residual: no /rom/system.json (always present on a provisioned board)
        info = {}  # hil-residual: bare fallback assign (system.json missing)
    try:
        import machine
        info["device_id"] = machine.unique_id().hex()
        log.debug("identity: device id")              # HIL path witness (unique_id read)
    except (ImportError, AttributeError):  # hil-residual: no machine.unique_id/hex (always present on a real port)
        pass  # hil-residual: bare pass (no unique_id/hex on this port)
    log.debug("identity: ready")                      # HIL path witness (runs every check-in)
    return info  # hil-residual: bare return of the identity dict


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


def _checkin_body(info, st, slot_states=None):
    """The base check-in payload from identity() + status() (+ slots()) -- pure, so it's
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
        # BOTH slots, newest first. The fields above describe the image that is RUNNING; these
        # describe what the device would fall back to, which is the thing a fleet operator
        # cannot infer from the running image and the thing A/B made worth knowing. An older
        # server ignores the key; a single-image device sends one entry.
        "slots": list(slot_states or []),
    }


def _defer_install(st, slot_states):
    """Why an offered update must WAIT, or ``None`` to go ahead.

    ONE rule, and it only exists under A/B: **do not install while the running image is an
    unconfirmed trial.** The installer writes the slot we are not running -- which, during a
    trial, is the slot holding the last release known to work. Taking a new update then trades
    a proven fallback for an unproven one, and it does it at the exact moment the device has
    already told us it is unsure of itself.

    Costs are lopsided again. Waiting costs one poll interval, and the wait ends the moment the
    app calls ``confirm()``. Not waiting costs the fallback, and only matters when the update
    also turns out to be bad -- i.e. precisely when you needed it.

    In SINGLE mode there is no fallback to protect and nothing to wait for, so the rule does
    not apply: gating there would strand a one-slot device on a trial it cannot leave."""
    if len(slot_states) < 2:
        return None
    if st.get("trial"):
        return "running an unconfirmed trial"
    return None


def _collect_body(info, st, slot_states=None):
    body = _checkin_body(info, st, slot_states)
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
              ca=None, ntp_host=None, recover=None,
              recover_after=3):  # pragma: no cover  (device: the network loop)
    """The OTA lifecycle loop (async, so it coexists with the app's asyncio work
    and openmv_cloud's background tasks). Forever: resolve the clock, poll the
    update server, hand the response to registered extensions (the live + ingest
    grants flow to openmv_cloud here), install any offered update, and back off.
    Never returns.

    Confirming a freshly-installed update (promoting it off trial so it can't roll
    back) is the APP's call, not run()'s: run() does NOT auto-confirm, because "it
    booted far enough to start run()" is a weak health signal. Your app calls
    ``openmv_ota.confirm()`` once it is actually operational (the generated main.py
    does this in its loop). ``self_test`` is an OPTIONAL boot-time shortcut: pass a
    function and run() confirms at boot iff it returns True -- for apps whose health
    is knowable immediately and that would rather not confirm in their own loop.
    Leave it None (the default) to confirm explicitly.

    ``ca`` are TLS anchors (PEM/path); ``None`` uses the bundled ``data/ca.pem``.
    ``ntp_host`` overrides the NTP server used to set the clock when the RTC is
    not already trustworthy (``None`` = ntptime's default pool).

    ``recover`` is how a device gets ITSELF out of a WEDGED NETWORK STACK. Retrying
    a check-in forever is not a recovery strategy: a stack can enter a state where
    every socket call fails identically no matter how long you wait (measured on the
    ATWINC1500 -- 39 consecutive ``OSError(22)`` EINVAL check-ins after a reset
    landed mid-transfer; it never came back on its own). Nothing in the poll loop
    re-initialises the interface, because run() does not own it -- the app brings the
    network up. So after ``recover_after`` CONSECUTIVE failed cycles run() calls this
    hook and the app re-initialises its own transport; a coroutine is awaited, so the
    generated ``main.py`` can pass its ``bring_up_network`` directly. Re-creating the
    NIC object is what actually clears a wedge -- on the WINC that path hard-resets
    the chip (``winc_init`` -> ``nm_bsp_reset``, EN/RST low) -- but this is deliberately
    transport-AGNOSTIC: the same escalation serves a stuck cyw43, a dead LAN link, or a
    router that came back on a different subnet. ``None`` keeps the old behaviour
    (retry forever, never re-initialise).

    The counter tracks CONSECUTIVE failures and resets on any completed cycle, so a
    flaky link that still gets through now and then never triggers it."""
    import asyncio  # hil-residual: the restart backoff awaits; imported here for the same reason _poll_forever imports its own
    while True:  # hil-residual: the RESTART loop emits nothing on the happy path -- every marker comes from _poll_forever inside it
        try:  # hil-residual: guard only; a healthy loop never leaves it
            await _poll_forever(server_url, self_test, poll_after_s, ca, ntp_host,  # hil-residual: delegates to the loop; witnessed by run.* markers throughout
                                recover, recover_after)
        except Exception as e:  # hil-residual: reached only when the OTA loop is dying, which is precisely the case that otherwise leaves no trace
            # NEVER DIE PERMANENTLY. run() is an asyncio TASK, and MicroPython reports a dead task
            # to the REPL, not to our logger -- so an OTA loop killed by anything the body does not
            # catch simply STOPS: the app keeps running, the board looks healthy, and the device
            # never updates again with nothing in the log to say why. Measured on an N6: one
            # `run: OTA LOOP DIED OSError(2,)` on a post-bite boot and the OTA path was gone for
            # good. The loop's own setup (CA resolve, status read) sits OUTSIDE its while, so a
            # single transient error there was fatal rather than something to retry.
            # So: log it, wait a poll, and start over. Nothing an OTA device does is worth giving
            # up the ability to be updated.
            log.error("run: OTA LOOP DIED %r -- restarting" % (e,))  # hil-residual: THE witness for a dead OTA loop; no bench scenario kills it on purpose, so it is unexercised -- which is exactly why it must exist before one does
            await asyncio.sleep(poll_after_s)  # hil-residual: back off one poll before re-entering
        except BaseException as e:  # hil-residual: cancellation/shutdown -- record, then let it through
            # NOT restarted: CancelledError and KeyboardInterrupt mean somebody is deliberately
            # stopping us (asyncio shutdown, or a probe taking the REPL). Restarting through those
            # would fight the caller. Still logged, because on the bench this is what a harness
            # Ctrl-C looks like and it was previously indistinguishable from a hang.
            log.error("run: OTA LOOP STOPPED %r" % (e,))  # hil-residual: witnessed only when something cancels the task
            raise  # hil-residual: re-raise so cancellation/shutdown still behave


async def _poll_forever(server_url, self_test, poll_after_s, ca, ntp_host, recover,
                        recover_after):  # pragma: no cover  (device: the network loop)
    """run()'s whole body, split out ONLY so run() can wrap it in one handler -- see there."""
    import asyncio
    boot = status()
    if boot.get("trial") and self_test is not None and self_test():
        confirm()  # hil-residual: opt-in boot-time self_test confirm; bench apps confirm in their loop (confirm.promoted), not via self_test, so this call-site is unexercised
    here = __file__.rsplit("/", 1)[0]
    ca = _resolve_ca(ca, here)
    fails = 0                             # CONSECUTIVE failed cycles; drives the recover escalation
    while True:
        wait = poll_after_s
        _resolve_clock(ntp_host)          # cheap once trusted; retries NTP until network is up
        # SPLIT ON PURPOSE: a failed CHECK-IN is a transport fault, everything after it is a
        # verdict on the release. Only the first kind may drive the recover escalation.
        try:
            # The check-in is a BLOCKING socket op (handshake + tiny response) -- a blocking mbedtls
            # C call that does not yield to asyncio, so it freezes the event loop (and any app feed
            # loop) longer than a short watchdog window -> relax() ISR-feeds across it. A no-op
            # unless the app armed a watchdog. Blocking (settimeout, no poll) is also required for
            # the WINC1500 (see _checkin): asyncio's poller raises EIO on WINC sockets.
            # NOTE: a park HERE is not recoverable in software -- see openmv_wdt; only an armed
            # hardware watchdog gets the board back, which is what relax()'s budget now allows.
            st = status()
            slot_states = slots()
            with _wdt_relax():  # hil-residual: watchdog-off CM is a no-op on the bench's default runs; the ENABLED watchdog scenario exercises the ISR-feed
                resp = _checkin(server_url, _collect_body(identity(), st, slot_states), ca)
        except Exception as e:  # hil-residual: check-in transport failure (the wedge path)
            # THE TRANSPORT IS SUSPECT. This is the failure a wedged stack produces every poll,
            # forever (measured: 39 consecutive EINVAL check-ins on an ATWINC1500), so it is the
            # only one allowed to escalate to recover().
            log.warning("run: cycle failed %r" % e)  # hil-residual: transient-failure witness
            fails += 1  # hil-residual: counter arithmetic; the COUNT is witnessed downstream -- N `run: cycle failed` lines followed by exactly one `run: recovering transport` is what proves the streak logic on HW
            if recover is not None and fails >= recover_after:  # hil-residual: the taken branch is witnessed by `run: recovering transport`; the not-taken branch by its ABSENCE after fewer than recover_after failures
                fails = 0                 # one escalation per streak, not one per cycle after it  # hil-residual: witnessed by there being ONE `run: recovering transport` per streak of failures, not one per poll after the threshold
                await _recover(recover)  # hil-residual: the witness for this call is emitted by the CALLEE's first line (`run: recovering transport`); the audit cannot see across the call boundary, and a marker here would duplicate it
        else:
            # The check-in got through, so the transport WORKS -- whatever happens next is about
            # the release, not the link. Forget the streak, and never let a rejected update look
            # like a wedged network: a bad signature, an unknown key or a failed anti-rollback
            # repeats every poll for as long as that release is offered, and counting those would
            # have the device tearing down and rebuilding its network forever over an image that
            # is never going to validate. On the WINC that rebuild is a full chip reset. (Measured
            # on the bench: bad_sig / bad_key / bad_version each drove a spurious recover.)
            fails = 0  # hil-residual: the streak RESET is witnessed by absence -- a healthy board polls for a whole run and never emits `run: recovering transport`; a marker here would fire every poll and drown the log
            try:
                log.debug("checkin: response received")
                _notify(resp)
                wait = resp.get("poll_after_s", poll_after_s)
                manifest_url = _offer(resp)
                if manifest_url:
                    log.debug("checkin: update offered")
                    defer = _defer_install(st, slot_states)  # hil-residual: the DEFER path needs a device to be mid-trial at the moment an update is offered, which no current scenario produces (the bench apps confirm as soon as they boot) -- the scenario for it lands with the step-6 catalog rework
                    if defer:  # hil-residual: see above; the not-taken branch is witnessed by install.start on every install scenario
                        # Not a failure and not a rejection -- the offer stands, we are simply
                        # not in a position to take it yet. Says WHY, because a device that
                        # polls, is offered an update, and does nothing is otherwise
                        # indistinguishable in the log from one that is broken.
                        log.info("checkin: deferring the update (%s)" % defer)  # hil-residual: emitted only on the deferred path above (no scenario reaches it yet); it is a field diagnostic until the step-6 defer scenario exists
                    else:
                        install(manifest_url, ca)  # hil-residual: install() reboots on success (no post-return witness); that it ran is proven by install.start / install.staged
            except Exception as e:  # hil-residual: post-check-in failure (a verdict on the release, or an install fault); exercised by corrupt/bad_sig
                # Retry next poll -- but SAY SO. Swallowed silently, a board that can never install
                # (e.g. the installer read blowing the heap) is indistinguishable on the wire and in
                # the log from a board with nothing on offer: the same check-in, the same poll wait,
                # forever. Bounded: one repr of the exception, no traceback buffer.
                log.warning("run: cycle failed %r" % e)  # hil-residual: transient-failure witness
        _wdt_feed()
        log.debug("run: poll wait")                  # HIL path witness (loop tail; _wdt_feed fed)
        await asyncio.sleep(wait)  # hil-residual: bare loop-tail await (sleep only; nothing follows)


async def _recover(recover):
    """Ask the app to re-initialise its transport, and NEVER let that fail the loop.

    A recover hook runs when the network is already broken, so it is the single most
    likely thing in the loop to raise -- and if it did, it would escape run()'s own
    ``except`` (which has already been left) and kill the OTA task outright, turning a
    recoverable wedge into a permanently un-updatable device. So it is wrapped here.

    The hook's work happens inside ``recover()`` when it is a plain function -- a NIC
    re-init is a long blocking C op (the WINC's chip reset alone sleeps 300 ms), far
    past a 100 ms watchdog window -- so that call runs under ``relax()``. An async hook
    only BUILDS its coroutine there and does the work in the ``await``, which yields to
    asyncio and lets the app's own feed loop run, so the await is deliberately OUTSIDE
    the relax: holding relax across an await would disable the watchdog for as long as
    the app felt like taking."""
    log.warning("run: recovering transport")      # HIL witness + field diagnostic
    try:
        with _wdt_relax():
            res = recover()
        if hasattr(res, "send"):                  # a coroutine/generator -> an async hook
            await res
        log.info("run: transport recovered")      # HIL witness: the hook returned cleanly
    except Exception as e:
        log.warning("run: recover failed %r" % e)  # bounded: one repr, no traceback buffer


def _resolve_clock(ntp_host):  # pragma: no cover  (device: RTC + network)
    """Establish a trustworthy wall clock so records can carry real timestamps.
    A no-op once the clock is good (the deep-sleep / coin-cell case resolves on
    the first pass with no network); otherwise it retries NTP each poll until the
    network is up. Defensive: a missing clock module or a failed sync just leaves
    timestamps absent -- ``seq`` still orders every record."""
    try:
        import openmv_rtc
        # An NTP sync is a BLOCKING network op the main loop cannot feed through, so it must relax()
        # the watchdog exactly like the check-in does. Without this it was the ONLY unfed blocking
        # call left in the poll loop, and it reset-looped the board: with the watchdog armed at
        # 100 ms, `clock: syncing` was the last line before every reboot, with reset_cause=3 (WDT).
        # It is worst on a network that BLACKHOLES NTP -- each unreachable server burns its full
        # socket timeout, and sync() walks a fallback list -- which is precisely when a device most
        # needs to stay alive. A no-op once the clock is trusted (the common case: no relax at all).
        with _wdt_relax():
            openmv_rtc.resolve(ntp_host)
        log.debug("clock: resolved")                  # HIL path witness (NTP/RTC each poll)
    except Exception:  # hil-residual: clock-unresolved wrapper (missing module / failed NTP)
        pass  # hil-residual: bare pass; clock left unresolved


def _read_capped(sock, limit, clen=None):  # pragma: no cover  (device network)
    """Read a response body, capped at ``limit``. When the Content-Length ``clen`` is known, read
    EXACTLY that many bytes and stop -- never wait for EOF: the WINC1500 reports a peer close only
    after the socket timeout elapses, so an EOF-terminated read stalls a whole _CHECKIN_TIMEOUT every
    poll. Without a length (chunked / connection-close), fall back to read-to-EOF. Never ``read(-1)``:
    a captive portal or broken proxy must not size our allocation. Bounded chunks joined once."""
    if clen is not None and clen > limit:
        raise OSError("check-in response over %d bytes" % limit)  # hil-residual: bare raise (declared over cap)
    chunks, total = [], 0
    while clen is None or total < clen:
        d = sock.read(_CHUNK if clen is None else min(_CHUNK, clen - total))
        if not d:                                     # EOF (peer closed) -- a short body, or the no-length case
            break  # hil-residual: EOF before the declared length -- a TRUNCATED body. Every server we talk to sends Content-Length (witnessed by run.checkin_clen), so the loop normally exits on total==clen and this fires only when the peer closes early; reaching it needs a fault-injected short response
        total += len(d)
        if total > limit:
            raise OSError("check-in response over %d bytes" % limit)  # hil-residual: bare raise (over-cap guard, inject-only)
        chunks.append(d)
        log.debug("checkin: body chunk")              # HIL path witness (a body chunk accumulated)
    body = b"".join(chunks)                            # bounded: sum of capped chunks <= limit
    log.debug("checkin: body read")                   # HIL path witness (response body complete)
    return body  # hil-residual: bare return of the joined body


def _checkin(server_url, body, ca):  # pragma: no cover  (device network)
    """POST the check-in body to ``/api/v1/check`` and return the parsed JSON.

    Transport is a BLOCKING, ``settimeout()``'d socket + ``ssl.wrap_socket`` -- NOT asyncio streams.
    The WINC1500 (the H7's wifi shield) implements no ``poll``/``select``, so asyncio's poller raises
    ``OSError(EIO)`` on its sockets; a settimeout socket needs no poll and is the transport OpenMV
    recommends for the WINC. It behaves identically over WLAN/LAN, so the N6/RT/AE3 are unaffected.
    The connect + handshake + small response run under the caller's ``_wdt_relax()`` (ISR feed) --
    unchanged, since the handshake was always a blocking mbedtls call that never yielded to asyncio
    anyway; only the tiny response read is newly blocking, and it is bounded by ``_CHECKIN_TIMEOUT``.
    Mirrors the installer's ``_connect`` (getaddrinfo -> settimeout -> connect -> wrap_socket)."""
    import json
    import socket
    import ssl
    scheme, _, rest = server_url.rstrip("/").partition("://")
    hostport, _, _ = rest.partition("/")
    host, _, port = hostport.partition(":")
    port = int(port) if port else (443 if scheme == "https" else 80)
    ai = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)[0]
    sock = socket.socket(ai[0], ai[1], ai[2])
    ss = None
    try:
        sock.settimeout(_CHECKIN_TIMEOUT)            # bounds handshake + each recv; WINC-safe (no poll)
        sock.connect(ai[-1])
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(cadata=ca.decode() if isinstance(ca, bytes) else ca)
        ss = ctx.wrap_socket(sock, server_hostname=host)   # blocking TLS handshake (ISR-fed by caller)
        payload = json.dumps(body).encode()
        ss.write((
            "POST /api/v1/check HTTP/1.1\r\nHost: %s\r\nUser-Agent: openmv-cam/1.0\r\n"
            "Content-Type: application/json\r\nContent-Length: %d\r\n"
            "Connection: close\r\n\r\n" % (host, len(payload))).encode() + payload)
        status_line = ss.readline()
        if b" 200 " not in status_line and not status_line.rstrip().endswith(b" 200"):
            raise OSError("check-in HTTP %s" % status_line)  # hil-residual: bare raise (non-200; happy path is 200)
        log.debug("checkin: server ok")              # milestone + HIL path witness
        clen = None
        left = _RESP_HEADERS_MAX                     # CEILING THE HEADER COUNT. Each readline is
        #                                              bounded by the socket timeout, but the LOOP was
        #                                              not: a server (or a captive portal) that drips
        #                                              one header line per timeout keeps the check-in
        #                                              alive indefinitely. Nothing accumulates in RAM
        #                                              here -- the lines are discarded -- so this
        #                                              bounds TIME, the same thing relax()'s budget
        #                                              bounds one level up.
        while True:                                  # skip headers, noting Content-Length
            line = ss.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            left -= 1
            if left <= 0:
                raise OSError("check-in sent over %d headers" % _RESP_HEADERS_MAX)  # hil-residual: over-cap guard, inject-only -- no real server sends 64 headers, so reaching it needs a fault-injected reply; the raise surfaces as run's `run: cycle failed` witness
            if line[:15].lower() == b"content-length:":   # read exactly this -> no EOF-wait on the WINC
                try:
                    clen = int(line.split(b":", 1)[1].strip())
                    log.debug("checkin: content length")  # HIL path witness (the server declared a length,
                    #                                       so the body read is exact -- every board, every poll)
                except Exception:  # hil-residual: malformed length -> fall back to read-to-EOF
                    clen = None  # hil-residual: bare assign
        resp = json.loads(_read_capped(ss, _RESP_MAX, clen))
        log.debug("checkin: parsed")                  # HIL path witness (headers skipped + JSON)
        return resp  # hil-residual: bare return of the parsed response
    finally:
        try:
            (ss or sock).close()                      # closing the TLS wrapper closes the raw socket
        except Exception:  # hil-residual: best-effort close of the check-in socket
            pass  # hil-residual: bare pass; nothing else holds the fd
        log.debug("checkin: closed")                  # HIL path witness (connection closed)


def _advance_rollback(cfg, slot, version):  # pragma: no cover (device)
    """Raise the anti-rollback floor to ``version`` by appending it to the RUNNING slot's
    rollback sector (a 1->0 program, no erase). A no-op if the floor already covers
    ``version`` or the log is full (the floor then stays frozen at its max -- still
    protective).

    v1 wrote this into BACK, which worked only because BACK was permanent. Under A/B every
    slot is erased in turn, so the floor is written into the slot being confirmed and boot.py
    reads the max across slots; the installer carries the floor forward into each new slot, so
    it survives the slot that recorded it being rewritten."""
    import uctypes
    import vfs
    base = uctypes.addressof(vfs.rom_ioctl(2, 0))
    soff, size = _slot_bounds(cfg, slot)
    off = soff + size - 3 * cfg.CONTROL_BLOCK            # this slot's rollback sector (absolute)
    sector = uctypes.bytearray_at(base + off, cfg.CONTROL_BLOCK)
    if _rollback_floor_of(sector) >= version:
        return  # hil-residual: bare early return (nothing to advance)
    pos = _rollback_append_offset(sector)
    if pos is None:
        return  # hil-residual: bare early return (floor already current)
    _write_verified(0, off + pos, _rollback_entry(version))
    log.debug("confirm: floor advanced")             # HIL path witness (the confirm write path)


def confirm():  # pragma: no cover
    """Keep the image we are RUNNING: raise the anti-rollback floor to this version, then
    write CONFIRMED into this slot -- iff it is an un-confirmed trial (a no-op otherwise).
    Advancing the floor *before* CONFIRMED means a crash in between leaves the floor raised
    but the image un-confirmed, so the next boot safely falls back to the previous slot
    (which the floor never locks out, because that image is at or above it). Everything here
    is addressed by the RUNNING slot, so there is no way to confirm a trial we fell back from.
    Returns True iff it just confirmed; raises OSError if a write fails. Idempotent -- safe to
    call every boot once healthy."""
    import _ota_config
    slot, version, _r = _boot_result()
    off = _status_offset(_ota_config, slot)
    if not _should_confirm(slot, _read_at(0, off, 3 * MARKER_SIZE)):
        return False  # hil-residual: bare const return (not confirmable this boot)
    _advance_rollback(_ota_config, slot, version)
    _write_verified(0, off + _CONFIRMED_OFF, CONFIRMED)
    log.info("confirm: kept the running image (slot %s)" % slot)
    return True  # hil-residual: bare const return (confirmed)


# A resource handler is a ``(matches, apply)`` pair, both taking ``(entry, path)`` --
# ``entry`` is the resources.json record (so each handler reads its own args) and
# ``path`` is the bundled data file. ``matches`` is the idempotence check ("already
# applied?"); ``apply`` does the write. sync() is handler-agnostic, so a future
# resource kind (keys, fuses, ...) is just another entry in _HANDLERS -- no partition
# assumptions baked into the loop.

def _partition_matches(entry, path):  # pragma: no cover  # hil-residual-fn: coprocessor partition path; AE3 HW-blocked (no working HIL coproc rig)
    """matches() for the ``partition`` handler: stream-compare the file to the start of
    partition ``entry["partition"]`` (the partition via a uctypes view, the file one
    chunk at a time -- neither whole image in RAM)."""
    import uctypes
    import vfs
    base = uctypes.addressof(vfs.rom_ioctl(2, entry["partition"]))
    same = _streams_equal(_file_chunks(path),
                          lambda off, n: uctypes.bytearray_at(base + off, n), _wdt_feed)
    log.debug("partition: compare")                   # idempotence check ran (every sync)
    return same


def _partition_apply(entry, path, progress=None):  # pragma: no cover  # hil-residual-fn: coprocessor partition path; AE3 HW-blocked (no working HIL coproc rig)
    """apply() for the ``partition`` handler: erase + program partition
    ``entry["partition"]`` with the file, streamed in _CHUNK blocks (never the whole
    image in RAM). The final block is 0xFF-padded to a full chunk -- matching the erased
    flash, and ignored since the romfs is self-sized. ``progress(done, total)`` (if given)
    reports the write's advance per chunk."""
    import os
    part_index = entry["partition"]
    size = os.stat(path)[6]
    total = (size + _CHUNK - 1) // _CHUNK * _CHUNK
    with _wdt_relax():                                 # WRITE_PREPARE: erases a NOR partition;
        _rom_write(3, part_index, total)              # a NO-OP on byte-writable MRAM (the AE3
                                                       # coprocessor partition), which never reads
                                                       # back 0xFF -- so DON'T verify-erased here
    log.debug("partition: prepared")                  # (the per-chunk write read-back below is the
                                                       # real integrity check, as the installer does)
    off = 0
    _first = True
    for chunk in _file_chunks(path):
        if len(chunk) < _CHUNK:
            chunk = chunk + b"\xff" * (_CHUNK - len(chunk))
        # FEED IMMEDIATELY BEFORE THE FLASH OP, not after it. A program/verify runs with
        # interrupts disabled on some ports (mimxrt flash.c), so nothing -- not this feed, not
        # relax()'s ISR -- can run once it starts; the only thing that helps is entering it on a
        # FULL window. Feeding afterwards instead handed each write whatever was left after the
        # previous write, the 0xFF padding above (an allocation, and an allocation can trigger a
        # collect -- 243 ms measured on an RT1060 with a full heap), the file read, and the
        # caller's `progress` callback, whose duration we do not control at all.
        _wdt_feed()
        _write_verified(part_index, off, chunk)       # WRITE one block + verify
        if _first:                                    # witness the write-loop body once (no spam)
            log.debug("partition: writing")
            _first = False
        off += _CHUNK
        if progress is not None:
            progress(off if off < total else total, total)
    _wdt_feed()                                        # ...and before the flush, which is another
    #                                                    unfeedable flash op and follows the
    #                                                    caller's progress callback
    _rom_write(5, part_index)                          # WRITE_COMPLETE: flush cached sub-page
                                                       # writes so they survive reset (NOR/XIP
                                                       # ports cache them; no-op on MRAM), exactly
                                                       # as the installer's complete() does


# resource kind -> (matches, apply); add new kinds here without touching sync().
_HANDLERS = {"partition": (_partition_matches, _partition_apply)}


def _data_path(name):  # pragma: no cover
    # __file__ is the package's __init__.py (a full path on MicroPython); the data
    # files sit beside it under data/. (MicroPython's __path__ is a str, not a list,
    # so derive the dir from __file__ instead.)
    path = __file__.rsplit("/", 1)[0] + "/data/" + name
    log.debug("data: path")                           # HIL path witness (sync() locates data/*)
    return path  # hil-residual: bare return of the data path


def sync():  # pragma: no cover  # hil-residual-fn: coprocessor partition path; AE3 HW-blocked (no working HIL coproc rig)
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
            log.debug("sync: already applied")        # idempotent skip (partition matches bundle)
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
    then stream that image into the slot the device is not running, arm the trial, and
    reboot.

    ``url`` is the **manifest** (produced by ``build ota-romfs``), not a raw image -- the
    device resolves the actual image location from the signed manifest internally. It is
    either an ``https://`` URL, or a **file path** (``"/sd/fw/OPENMV_N6-manifest.bin"``)
    with the published artifacts beside it on a mounted filesystem. The
    verification is identical either way -- signature, vetting, anti-rollback -- because
    the medium is untrusted in both cases and the signature is the boundary. Does
    **not** return on success -- it reboots. A failure *after* the write commits reboots
    into the previous working image instead (boot.py rejects the half-written slot); a
    pre-flight failure (bad URL or path, DNS, TLS, a bad/forbidden/rolled-back manifest)
    raises before anything is erased, so the app can catch it and retry without a reboot.
    Call after any app teardown -- the install erases ``/rom``, so the running app cannot
    continue past this call -- and, for a URL, once the network is up (WiFi/Ethernet/
    HaLow); a file install needs no network at all.

    The heavy lifting lives in ``data/installer.py``, shipped as source and ``exec``'d
    into RAM here: in single-image mode the slot being erased is the one the app runs from, so the
    installer must run from RAM, not XIP from that slot. For that same reason install
    progress is *logged* by the installer (RAM + the frozen logger), not delivered to a
    caller callback -- any callback here (this lib, the app) lives in the slot being
    erased, so calling it post-erase would XIP from erased flash. (``sync()`` *does* take
    an ``on_progress`` -- it erases a different partition, leaving this one intact.) ``ca``
    are the TLS trust anchors (PEM): ``None`` uses the bundled ``data/ca.pem`` (the Mozilla
    root bundle), ``bytes`` are used as-is, and a ``str`` is a path to read. Ignored for
    a file install -- there is no connection to authenticate."""
    import _ota_config as cfg
    here = __file__.rsplit("/", 1)[0]
    ca = _resolve_ca(ca, here) if "://" in url else None  # a file install has no TLS peer
    ns = {}
    # exec()'ing the ~1000-line installer source is one unsplittable compile+exec that can exceed a
    # short watchdog window; relax() ISR-feeds across it (no-op unless the app armed a watchdog). This
    # runs synchronously in run()'s task, so the app's async feed loop can't cover it.
    with _wdt_relax():
        try:
            exec(_read_file(here + "/data/installer.py", "r"), ns)
            run = ns["run"]
            log.debug("install: staged installer")   # milestone + HIL path witness
        except MemoryError:  # hil-residual: small-heap fallback; measured on the bench (F427 = 39,120B free TOTAL), unreachable on the A/B fleet's boards
            # The failed exec leaves the read source (and half-built module dict) as
            # garbage -- on a 47 KB heap that IS the heap. Reclaim it before the
            # frozen installer needs room for its inflate window.
            ns = {}  # hil-residual: drops the failed exec's garbage; classic-only path, no fleet marker
            import gc  # hil-residual: classic-only fallback arm (same witness as the frozen import below)
            gc.collect()  # hil-residual: the collect that makes the frozen installer's window fit on a ~40 KB heap
            # A small-heap board (an F427 has ~39 KB of heap, TOTAL) can never exec
            # the installer source into RAM, however small it is packed. The firmware
            # already freezes the SAME source as `openmv_installer` for recovery, and
            # frozen bytecode runs from flash with ~zero heap -- use that copy. The
            # exec path stays first because the romfs copy is OTA-patchable; this
            # fallback self-selects on exactly the boards that need it.
            import openmv_installer  # hil-residual: frozen module (firmware-resident); no bench marker until a classic joins the fleet
            run = openmv_installer.run  # hil-residual: same-source binding (the freeze copies data/installer.py verbatim)
            log.info("install: staged frozen installer (exec would not fit)")  # hil-residual: witnessed on classic boards only
    run(url, ca, cfg)  # hil-residual: terminal call into the installer (reboots on success, no post-return witness); that it ran is proven by install.download / install.writing / install.armed


def _resolve_ca(ca, base):  # pragma: no cover
    """Normalise the TLS trust anchors run()/install() accept -- ``None`` -> the bundled
    ``data/ca.pem``, a ``str`` -> a path to read, ``bytes`` -> used as-is. Shared so both
    entry points normalise identically (and so the read is witnessed in one place)."""
    if ca is None:
        # THE TRUST STORE LIVES IN THE FIRMWARE, not the romfs. Frozen, it is read straight out
        # of flash -- no RAM copy of a ~186 KB bundle -- and it costs the slot nothing. That last
        # part is what makes it matter: the romfs image is duplicated under A/B, so a bundle
        # shipped there is paid for TWICE, and on a single-image board (M4/M7/H7 classic, whose
        # whole slot is 114688 bytes) it did not fit at all. It is also the same reasoning that
        # puts boot.py and openmv_log in the firmware: a device must be able to reach its update
        # server even when the filesystem holding the app is the thing that is broken.
        try:  # hil-residual: the import guard itself; both arms are witnessed (ca: frozen / the legacy read), the `try` cannot carry its own marker
            import openmv_ca  # hil-residual: dominated by `ca: frozen` on the next line but one
            ca = openmv_ca.PEM  # hil-residual: dominated by `ca: frozen` on the next line
            log.debug("ca: frozen")  # hil-residual: the bench server is self-signed, so every leg passes an EXPLICIT ca (path or bytes) and none reaches the default frozen store
        except ImportError:  # hil-residual: only a project scaffolded BEFORE the store moved reaches this; current firmware freezes openmv_ca
            # A project scaffolded before the store moved still ships it in the romfs. Keep
            # reading that, so an existing project keeps updating across the change.
            ca = _read_file(base + "/data/ca.pem", "rb")  # hil-residual: legacy romfs-ca branch; the bench passes an explicit CA (self-signed server) and current firmware freezes openmv_ca, so no leg reaches it
    elif isinstance(ca, str):
        ca = _read_file(ca, "rb")
        log.debug("ca: from path")                    # HIL path witness (run() passes a CA path)
    else:
        log.debug("ca: bytes")                        # HIL path witness (install() gets bytes from run())
    return ca  # hil-residual: bare return of the resolved anchors


def _read_file(path, mode, limit=_ASSET_MAX):  # pragma: no cover
    """Read one of our OWN shipped assets (installer.py, ca.pem) whole -- they
    have to be whole to exec()/parse. Still bounded: these are fixed build
    artifacts, so exceeding the ceiling means a corrupt romfs, not a big input.

    Sized by the file's ACTUAL length, never by the ceiling. MicroPython's
    ``f.read(n)`` pre-allocates n bytes up front, so asking for the 256 KiB limit
    demanded a 256 KiB contiguous block to read a 68 KiB installer -- a
    MemoryError on every board with no external SDRAM. Measured on a Nicla
    Vision sitting idle at 350 KiB free: ``f.read(_ASSET_MAX + 1)`` ->
    ``memory allocation failed, allocating 262145 bytes``, while the exact-size
    read returned all 69591 bytes. That raise landed inside ``install()``, so
    such a board could take the offer and then never install anything, forever.
    The ceiling is now a stat-time gate rather than an allocation size."""
    import os
    try:
        size = os.stat(path)[6]
    except OSError as e:  # hil-residual: missing-asset path; a passing run never takes it (every asset is present), so no marker can witness it -- the raise below is the witness when it does happen
        # NAME THE FILE. A bare `OSError(2,)` is what this used to surface, and on an N6 that
        # produced 161 identical lines with no way to tell WHICH asset was missing -- the OTA
        # loop died, restarted, and died again on the same file for the whole run. errno is
        # preserved so callers that classify on it still work; the path is what makes the log
        # actionable.
        raise OSError(e.args[0] if e.args else 0, "cannot read %s" % path)  # hil-residual: missing/unreadable asset; reaching it needs a genuinely absent file (measured on the bench when /flash lost the CA), not something a passing run hits
    if size > limit:
        raise OSError("%s exceeds the %d-byte asset ceiling" % (path, limit))  # hil-residual: bare raise (corrupt-romfs guard, inject-only)
    f = open(path, mode)
    try:
        data = f.read(size + 1)                       # +1: notice a file longer than stat claimed
        if len(data) > size:
            raise OSError("%s grew while being read" % path)  # hil-residual: bare raise (stat/read disagreement, inject-only)
        log.debug("asset: read")                      # HIL path witness (installer/ca asset read)
        return data  # hil-residual: bare return of the read asset
    finally:
        f.close()
        log.debug("asset: closed")                    # HIL path witness (asset file closed)

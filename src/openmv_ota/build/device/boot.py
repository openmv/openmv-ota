"""The frozen OTA ``boot.py`` -- the module openmv runs at boot.

This is the file the firmware build freezes into the image as ``boot.py``; on the
camera it runs after the board's stock ``_boot.py``. It selects the FRONT (mutable
runtime) or BACK (golden) ROMFS slot, verifies the slot's signed trailer (ECDSA
over the firmware's mbedtls, via an injected ``verify``), checks integrity /
cross-flash / compatibility / anti-rollback, runs the trial-boot status state
machine, and mounts the chosen slot.

The decision logic (``parse_trailer`` / ``evaluate_slot`` / ``OtaBoot``) is pure
and host-testable -- all flash I/O is injected. The **device entry** at the bottom
(``_main``) wires in ``vfs`` + the ECDSA C module + a build-generated ``_ota_config``
and auto-runs; on the host (and in tests) those imports are absent, so the module
stays inert and importable. So one file is both the camera's ``boot.py`` and a unit
under test -- there is no separate logic module on the device.

The on-flash format mirrors :mod:`openmv_ota.ota.trailer`, :mod:`openmv_ota.ota.status`,
and :mod:`openmv_ota.ota.geometry`. This module cannot import them (it runs under
MicroPython), so the constants are duplicated here and ``test_device_boot`` pins
them against the originals so they can't drift. Only struct/binascii/hashlib are
imported -- all present in CPython and MicroPython.

RAM BUDGET: this module runs inside your application, so its memory is your
memory. Every buffer here has a ceiling. Nothing is sized by a file's length, a
response body, a length field off the wire, or a queue that grows while the
network is down: reads use bounded windows of a few KB, anything larger is
streamed, and large data is aliased with memoryview/bytearray_at rather than
copied.
"""

import binascii
import hashlib
import struct

try:                                   # the firmware freezes openmv_log beside boot.py
    from openmv_log import log
except ImportError:                    # host / tests / a build without logging -> a null logger,
    class _NullLog:                    # so call sites never need an `is not None` guard
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

# --- Trailer format (mirror of openmv_ota.ota.trailer) ----------------------

MAGIC = b"OMVR"                         # ROMFS application image
HEADER_VERSION = 1
_HEADER_STRUCT = "<4sIIIIIIIIIIi32s"
_HEADER_SIZE = struct.calcsize(_HEADER_STRUCT)   # 80
_META_SIZE_OFFSET = struct.calcsize("<4sIII")    # 16
_CRC_SIZE = 4
# COSE alg id -> raw R||S signature length (mirror of openmv_ota.ota.algorithms).
_ALG_SIG_SIZE = {-7: 64, -35: 96, -36: 132}

# --- Status markers (mirror of openmv_ota.ota.status) -----------------------

MARKER_SIZE = 16
_PENDING_OFF = 0
_TRIED_OFF = 16
_CONFIRMED_OFF = 32
# --- v2 status fields -------------------------------------------------------
# Both live in the same status sector, past the four 16-byte markers (offset 64 on), and both are
# written WITHOUT erasing: the sector is blank (0xFF) after the slot erase and every write from
# then on only clears bits. That is what lets control data share an erase block with the body on
# the single-sector boards.
_COUNTER_OFF = 64                       # u32 value || u32 ~value -- a torn write is detectable
_COUNTER_LEN = 8
_ATTEMPTS_OFF = 72                      # one byte per boot attempt: 0xFF -> 0x00 as each is used
_ATTEMPTS_MAX = 64                      # ...capped; a slot that needs 64 boots is not coming back
DEFAULT_MAX_ATTEMPTS = 3                # boots a trial gets to confirm (openmv-ota.toml may lower it)


def _marker(label):
    return hashlib.sha256(b"openmv-ota.status." + label).digest()[:MARKER_SIZE]


PENDING = _marker(b"pending")
TRIED = _marker(b"tried")
CONFIRMED = _marker(b"confirmed")

# --- Anti-rollback floor (mirror of openmv_ota.ota.rollback) ----------------
_ROLLBACK_ENTRY = 8                     # u32 version || u32 ~version, in BACK's rollback sector


def _rollback_floor_of(sector):
    """The highest valid version recorded in a rollback sector (0 if none)."""
    floor = 0
    i = 0
    n = len(sector)
    while i + _ROLLBACK_ENTRY <= n:
        version, check = struct.unpack_from("<II", sector, i)
        if (version ^ 0xFFFFFFFF) == check and version > floor:
            floor = version
        i += _ROLLBACK_ENTRY
    return floor


class OtaReject(Exception):
    """A slot was rejected. The message is a short, stable reason code."""


class Trailer:
    """Decoded, structurally-valid trailer (no crypto applied yet)."""


def parse_trailer(data):
    """Structurally parse + CRC-check a trailer block, returning a :class:`Trailer`.

    Validates only what can be checked without keys -- magic, header version, a
    known algorithm with the matching signature length, framing, and the CRC. The
    header fields are NOT yet trustworthy: they become authentic only once
    :func:`evaluate_slot` verifies the signature over the signed region. Raises
    :class:`OtaReject` with a reason code on any malformation.
    """
    if len(data) < _HEADER_SIZE:
        raise OtaReject("trunc")
    (magic, header_version, body_size, pad_size, meta_size, sig_size, product_id,
     min_platform_version, payload_version, payload_version_floor, key_id, sig_alg,
     body_sha256) = struct.unpack_from(_HEADER_STRUCT, data, 0)
    if magic != MAGIC:
        raise OtaReject("magic")
    if header_version != HEADER_VERSION:
        raise OtaReject("version")
    expect_sig = _ALG_SIG_SIZE.get(sig_alg)
    if expect_sig is None or sig_size != expect_sig:
        raise OtaReject("alg")
    body_end = _HEADER_SIZE + meta_size + sig_size
    if body_end + _CRC_SIZE > len(data):
        raise OtaReject("trunc")
    crc_stored = struct.unpack_from("<I", data, body_end)[0]
    if (binascii.crc32(bytes(data[:body_end])) & 0xFFFFFFFF) != crc_stored:
        raise OtaReject("crc")

    t = Trailer()
    t.body_size = body_size
    t.pad_size = pad_size
    t.product_id = product_id
    t.min_platform_version = min_platform_version
    t.payload_version = payload_version
    t.payload_version_floor = payload_version_floor
    t.key_id = key_id
    t.sig_alg = sig_alg
    t.body_sha256 = bytes(body_sha256)
    t.signed_region = bytes(data[:_HEADER_SIZE + meta_size])
    t.signature = bytes(data[_HEADER_SIZE + meta_size:body_end])
    return t


def _sha256(data):
    h = hashlib.sha256()
    mv = memoryview(data)
    for off in range(0, len(mv), 4096):       # chunk so a multi-MB XIP body streams
        h.update(mv[off:off + 4096])
    return h.digest()


def _markers(status):
    return (status[_PENDING_OFF:_PENDING_OFF + MARKER_SIZE] == PENDING,
            status[_TRIED_OFF:_TRIED_OFF + MARKER_SIZE] == TRIED,
            status[_CONFIRMED_OFF:_CONFIRMED_OFF + MARKER_SIZE] == CONFIRMED)


def install_counter(status):
    """This slot's install counter, or ``None`` if unwritten/torn.

    A/B ordering hangs on this rather than on the image version. The version cannot order two
    slots when the SAME version is legitimately installed twice -- a re-install, or a re-flash of
    the same release -- and that is a case worth supporting rather than blocking. The counter says
    which install happened later and nothing else; the version stays a human-facing fact and an
    anti-rollback input.

    ``u32 value || u32 ~value`` so a half-written counter is *detected* rather than believed: an
    install that lost power partway must not be able to claim it is the newest."""
    raw = bytes(status[_COUNTER_OFF:_COUNTER_OFF + _COUNTER_LEN])
    if len(raw) < _COUNTER_LEN:
        return None
    value = int.from_bytes(raw[:4], "little")
    check = int.from_bytes(raw[4:], "little")
    if (value ^ 0xFFFFFFFF) != check:
        return None
    return value


def attempts_used(status):
    """How many boot attempts this slot has consumed (see ``attempt_offset``)."""
    region = bytes(status[_ATTEMPTS_OFF:_ATTEMPTS_OFF + _ATTEMPTS_MAX])
    used = 0
    for b in region:
        if b == 0xFF:
            break
        used += 1
    return used


def attempt_offset(status):
    """Offset of the next free attempt byte, or ``None`` when the region is full.

    An append region rather than a counter because flash cannot be incremented in place without
    an erase, and there is no erase available here -- the slot is erased once at install and every
    write after that is a 1->0 program. Consuming one byte per boot costs nothing and a torn write
    costs a single attempt instead of corrupting a count."""
    used = attempts_used(status)
    if used >= _ATTEMPTS_MAX:
        return None
    return _ATTEMPTS_OFF + used


def select_slot(candidates):
    """Pick which slot to boot from ``[(name, counter, confirmed), ...]`` -- already-validated
    slots only. Returns the winning name, or ``None`` when there is nothing bootable (recovery).

    Newest wins, and "newest" is the install counter: see ``install_counter`` for why the version
    cannot do this job. A slot whose counter is unreadable (blank or torn) is still bootable if it
    verified -- it just sorts last, because we cannot claim it is newer than something that says
    so. That matters for the very first boot after a factory flash, where nothing has a counter yet.

    Ties are possible in exactly two situations -- a factory flash that wrote both slots, and
    corruption -- so they get a defined answer rather than whatever order the caller passed:
    prefer a CONFIRMED slot (it is known to have run), then the first listed. Deliberately NOT the
    version: two slots holding the same version is the case this whole scheme exists to support."""
    best = None
    for name, counter, confirmed in candidates:
        if best is None:
            best = (name, counter, confirmed)
            continue
        bname, bcounter, bconfirmed = best
        # None sorts below any real counter
        if counter is None and bcounter is None:
            better = confirmed and not bconfirmed
        elif counter is None:
            better = False
        elif bcounter is None:
            better = True
        elif counter != bcounter:
            better = counter > bcounter
        else:
            better = confirmed and not bconfirmed
        if better:
            best = (name, counter, confirmed)
    return None if best is None else best[0]


def evaluate_slot(body, status, trailer_bytes, rollback_floor,
                  product_id, trusted_keys, platform_version, verify,
                  max_attempts=DEFAULT_MAX_ATTEMPTS):
    """Verify one slot and decide whether it may be mounted.

    Returns ``(trailer, consume_attempt)`` -- ``consume_attempt`` is True when this is a trial
    boot, telling the caller to burn one attempt byte before mounting.
    Raises :class:`OtaReject` (reason code) on any failure. ``body`` is the slot's
    body region (a memoryview on device); ``status`` is its status sector.

    The signature is verified *before* any header field is trusted -- ``product_id``,
    sizes, and versions are only acted on once the signature over ``header || meta``
    checks out.
    """
    t = parse_trailer(trailer_bytes)

    pubkey = trusted_keys.get(t.key_id)
    if pubkey is None:                                  # unknown or revoked key
        raise OtaReject("key")
    if not verify(t.sig_alg, pubkey, t.signature, t.signed_region):
        raise OtaReject("sig")
    # The header is authentic from here on.

    if product_id and t.product_id != product_id:             # cross-flash guard (0 = off)
        raise OtaReject("board")
    if t.min_platform_version and t.min_platform_version > platform_version:
        raise OtaReject("compat")
    if t.body_size > len(body):
        raise OtaReject("size")
    if _sha256(memoryview(body)[:t.body_size]) != t.body_sha256:
        raise OtaReject("body-sha")

    pending, _tried, confirmed = _markers(status)   # `tried` is superseded by the attempt region

    # SYMMETRIC. v1 required BACK to be "exactly the golden factory shape: confirmed only",
    # because BACK *was* the factory image and only FRONT was ever written. Under A/B both slots
    # are real, updatable images and either may be the newest, so the same rule has to hold for
    # both -- and the caller decides which one wins via select_slot(), not this function.
    if t.payload_version < rollback_floor:              # anti-rollback
        raise OtaReject("rollback")
    if confirmed:
        return t, False                                 # ran and was kept; boot it, consume nothing
    if pending:
        # A trial gets max_attempts boots to confirm, not one. The costs are lopsided: a FALSE
        # rejection costs a full re-download + erase + write that the server then offers again,
        # while an extra attempt on a genuinely bad image costs one reboot. See the plan.
        if attempts_used(status) >= max_attempts:
            raise OtaReject("trial-failed")             # spent its attempts, never confirmed
        return t, True                                  # trial: caller consumes one attempt
    # Neither confirmed nor pending: nothing was ever installed here (a blank slot, or a status
    # sector lost to a torn write). Not bootable.
    #
    # v1 also rejected "confirmed with no pending" as `forged-confirm`, on the grounds that it was
    # BACK's shape appearing on FRONT. That check is meaningless now: both slots use one shape, and
    # a factory-flashed slot legitimately looks exactly like that. It never bought much anyway --
    # forging a confirm skips the TRIAL, not the signature, so it needs flash-write access to make
    # an already-validly-signed image skip its probation.
    raise OtaReject("status")


class OtaBoot:
    """Wires the pure logic to a device's flash I/O and build-time constants.

    ``read``/``verify``/``mount``/``write_marker`` are the injected I/O; the rest
    are constants the firmware build bakes in (geometry + the trusted key set +
    this device's board id + the running firmware's platform version).
    """

    def __init__(self, read, verify, mount, write_marker, partition_size,
                 front_size, block, product_id, trusted_keys, platform_version,
                 max_attempts=DEFAULT_MAX_ATTEMPTS):
        self.read = read
        self.verify = verify
        self.mount = mount
        self.write_marker = write_marker
        self.partition_size = partition_size
        self.front_size = front_size
        self.block = block
        self.product_id = product_id
        self.trusted_keys = trusted_keys
        self.platform_version = platform_version
        self.max_attempts = max_attempts
        self._pending = {}

    def _slots(self):
        """``[(name, offset, size), ...]`` for this device's mode.

        A/B splits the partition into two slots OF EQUAL SIZE; SINGLE is one slot spanning all
        of it. Everything below is written against this list, so the two modes differ in exactly
        one place.

        Equal sizes are not cosmetic. One OTA image must be installable into EITHER slot -- the
        installer picks the target at run time -- and the image is slot-sized, with its trailer
        in the last block. A partition whose half does not divide evenly leaves a remainder
        below the split; that tail is deliberately unused (under one block per slot) rather
        than handed to B, which would make B's image a different size from A's."""
        if self.front_size <= 0 or self.front_size >= self.partition_size:
            return [("A", 0, self.partition_size)]          # SINGLE: one slot, whole partition
        return [("A", 0, self.front_size),
                ("B", self.front_size, self.front_size)]

    def _rollback_floor(self):
        """The anti-rollback floor: the highest version any slot has recorded as confirmed.

        v1 read this from BACK alone, which worked because BACK was the factory image and was
        never erased. Under A/B every slot is erased in turn, so a floor living in one slot would
        be lost the moment that slot is rewritten. Taking the max across both keeps it monotonic
        as long as an install carries the current floor into the slot it writes -- which is the
        installer's job (step 4), and the reason the floor is read from every slot here rather
        than from a designated one."""
        floor = 0
        for _name, offset, size in self._slots():
            logged = _rollback_floor_of(self.read(offset + size - 3 * self.block, self.block))
            if logged > floor:
                floor = logged
        return floor

    def _read_slot(self, offset, size):
        blk = self.block
        body = self.read(offset, size - 2 * blk)
        status = self.read(offset + size - 2 * blk, blk)
        trailer = self.read(offset + size - blk, blk)
        return body, status, trailer

    def run(self):
        """Mount the newest valid slot. Returns ``(slot, trailer, reason)`` where ``reason`` is
        the rejection of the slot NOT chosen (or None when there was only one candidate).

        Raises :class:`OtaReject('no-slot:...')` when nothing is bootable -- which under v2 is
        not the end of the road: there is no factory image to fall back to, so the caller hands
        to the firmware-resident recovery flow, which re-downloads until a working image exists.
        """
        floor = self._rollback_floor()
        candidates, rejects = [], []
        for name, offset, size in self._slots():
            body, status, trailer = self._read_slot(offset, size)
            try:
                t, consume = evaluate_slot(
                    body, status, trailer, floor, self.product_id, self.trusted_keys,
                    self.platform_version, self.verify, self.max_attempts)
            except OtaReject as e:
                rejects.append("%s:%s" % (name, e))
                continue
            _p, _tr, confirmed = _markers(status)
            candidates.append((name, install_counter(status), bool(confirmed)))
            self._pending[name] = (offset, size, body, status, t, consume)

        # Try the newest, and if it cannot be ARMED fall through to the next-newest rather than
        # abandoning the boot. Failing to record an attempt is a reason to distrust that slot, not
        # a reason to strand a device that has another valid image sitting right there.
        while True:
            winner = select_slot(candidates)
            if winner is None:
                raise OtaReject("no-slot:%s" % (",".join(rejects) or "empty"))
            try:
                return self._mount(winner, rejects)
            except OtaReject as e:
                rejects.append("%s:%s" % (winner, e))
                candidates = [c for c in candidates if c[0] != winner]

    def _mount(self, winner, rejects):
        offset, size, body, status, t, consume = self._pending[winner]
        if consume:
            # BURN THE ATTEMPT BEFORE MOUNTING. If the trial image hangs, nothing after this
            # point runs -- so an attempt recorded afterwards would never be recorded at all and
            # the same slot would be retried forever. Consuming it first is what makes a hang
            # count against the trial and eventually give up.
            off = attempt_offset(status)
            if off is None:
                raise OtaReject("trial-attempts-full")
            try:
                self.write_marker(offset + size - 2 * self.block + off, b"\x00")
            except OSError:
                # Cannot record the attempt, so cannot bound the trial. Running it anyway would
                # be an untracked trial: if it hung, the next boot could not tell to move on.
                raise OtaReject("trial-arm")
        self.mount(memoryview(body)[:t.body_size])
        return winner, t, (",".join(rejects) or None)


last_slot = None              # 'FRONT' or 'BACK'
last_payload_version = 0      # the mounted image's payload_version
last_failure_reason = None    # the FRONT rejection reason, if BACK was used


# --- Device entry -----------------------------------------------------------
# Wires vfs + the ECDSA C module + the build-generated _ota_config into OtaBoot
# and runs. Device-only: on the host these imports are absent, so the module is
# inert and the logic above stays importable for tests.

def _main(cfg):  # pragma: no cover  (hardware / QEMU only)
    import os
    import sys

    import uctypes
    import vfs

    try:
        from ecdsa_verify import verify   # the C module dropped into openmv/modules/
    except ImportError:  # hil-residual: coprocessor core (AE3 M55_HE) has no mbedtls; not HIL-runnable
        # A core that doesn't build mbedtls can't verify signatures, so it never runs
        # OTA: e.g. the Alif AE3 M55_HE helper core, which is slaved to the main core
        # and has its romfs written by it. Leave mp_init's stock /rom mount in place.
        return  # hil-residual: coprocessor-core-only path (slaved core, no HIL rig)

    # Read the XIP'd partition at each slot's *absolute* address via uctypes rather
    # than slicing one whole-partition memoryview: a memoryview's offset field is
    # 24-bit on 32-bit MicroPython, so mem[off:] overflows ("memoryview offset too
    # large") for any off >= 16 MiB -- which is the BACK slot on the 24 MiB N6/AE3
    # partitions. bytearray_at aliases the address with an internal offset of 0, so
    # the body's own [:body_size] slices below stay small regardless of the slot.
    part = vfs.rom_ioctl(2, 0)
    base = uctypes.addressof(part)                   # the partition's XIP base address
    # A block-device port (mimxrt) has no rom_ioctl WRITE (rom_ioctl(4) returns -EINVAL);
    # it exposes the partition as a Flash block device instead. Reads stay XIP (the whole
    # partition is memory-mapped, so bytearray_at works on every port), but the marker
    # WRITE must go through the block device. None on the XIP/ioctl ports (stm32/alif/samd).
    bdev = part if hasattr(part, "ioctl") else None

    _read_seen = []
    def read(off, size):
        body = uctypes.bytearray_at(base + off, size)    # the XIP alias -- witnessed below
        if not _read_seen:                               # witness the read once (no per-call spam)
            _read_seen.append(1)
            log.debug("boot: slot read")
        return body                                      # hil-residual: bare return of the read var

    def mount(body):
        vfs.mount(vfs.VfsRom(body), "/rom")
        log.debug("boot: slot mounting")             # AFTER the mount, so it witnesses vfs.mount

    def write_marker(off, marker):
        # Write, then read back and verify. A rejected (negative rc) or silently failed
        # write raises OSError, which _try_slot turns into a fall-back to BACK -- we
        # never run a trial we couldn't record. The status sector is pre-erased, so the
        # block-device path writes the marker byte-granularly (3-arg writeblocks, no erase).
        if bdev is not None:
            _bs = bdev.ioctl(5, 0)
            bdev.writeblocks(off // _bs, marker, off % _bs)
            log.debug("boot: marked slot block-device")
        else:
            if vfs.rom_ioctl(4, 0, off, marker) < 0:
                raise OSError("rom_ioctl write failed")  # hil-residual: write-fault (inject-only); the OSError->golden fallback is proven by rollback/corrupt
            log.debug("boot: marked slot XIP")
        if read(off, len(marker)) != marker:
            raise OSError("marker write verify failed")  # hil-residual: verify-fault (inject-only); fallback proven by rollback/corrupt
        log.debug("boot: slot marker verified")

    try:
        vfs.umount("/rom")           # drop mp_init's whole-partition auto-mount
    except OSError:
        log.debug("boot: no prior mount")   # mp_init didn't auto-mount /rom (blank/invalid romfs)

    try:
        slot, trailer, front_reason = OtaBoot(
            read, verify, mount, write_marker, cfg.PARTITION_SIZE, cfg.FRONT_SIZE,
            cfg.CONTROL_BLOCK, cfg.PRODUCT_ID, cfg.TRUSTED_KEYS, cfg.PLATFORM_VERSION).run()
    except OtaReject as e:
        log.error("boot: no bootable slot: %s" % e)
        raise  # hil-residual: bare re-raise to halt boot; the boot.no_slot log above is witnessed
    if front_reason is None:
        log.info("boot: mounted %s (payload %d)" % (slot, trailer.payload_version))
    else:
        log.warning("boot: FRONT rejected (%s) -> mounted %s (payload %d)"
                    % (front_reason, slot, trailer.payload_version))

    global last_slot, last_payload_version, last_failure_reason
    last_slot, last_payload_version, last_failure_reason = (
        slot, trailer.payload_version, front_reason)
    # Mirror onto _ota_config, the module the app's openmv_ota lib reads (both import it,
    # and modules are cached, so this persists in-VM without re-running boot.py).
    cfg.last_slot = slot
    cfg.last_payload_version = trailer.payload_version
    cfg.last_failure_reason = front_reason

    os.chdir("/rom")
    sys.path.append("/rom")
    sys.path.append("/rom/lib")
    log.info("boot: ready, running app")


try:                                   # the build generates _ota_config beside boot.py
    import _ota_config as _cfg
except ImportError:                    # host / tests: stay inert and importable
    _cfg = None
if _cfg is not None:
    _main(_cfg)  # pragma: no cover

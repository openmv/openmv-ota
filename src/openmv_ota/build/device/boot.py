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
    import openmv_log
except ImportError:                    # host / tests / a build without logging
    openmv_log = None

try:                                   # frozen HIL coverage markers; inert unless enabled
    import openmv_hilcov
except ImportError:                    # host / tests / a production build
    openmv_hilcov = None

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


def evaluate_slot(body, status, trailer_bytes, is_front, rollback_floor,
                  product_id, trusted_keys, platform_version, verify):
    """Verify one slot and decide whether it may be mounted.

    Returns ``(trailer, write_tried)`` -- ``write_tried`` is True only for a FRONT
    first trial boot, telling the caller to set the ``tried`` marker before mounting.
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

    pending, tried, confirmed = _markers(status)
    if not is_front:
        # BACK must be exactly the golden factory shape: confirmed only.
        if not (confirmed and not pending and not tried):
            raise OtaReject("back-not-factory")
        return t, False

    if t.payload_version < rollback_floor:              # anti-rollback vs BACK
        raise OtaReject("rollback")
    if pending and tried and confirmed:
        return t, False                                 # post-OTA confirmed
    if pending and not tried and not confirmed:
        return t, True                                  # one-shot trial: arm 'tried'
    if pending and tried and not confirmed:
        raise OtaReject("trial-failed")                 # tried but never confirmed
    if confirmed and not pending and not tried:
        raise OtaReject("forged-confirm")               # BACK shape on FRONT
    raise OtaReject("status")


class OtaBoot:
    """Wires the pure logic to a device's flash I/O and build-time constants.

    ``read``/``verify``/``mount``/``write_marker`` are the injected I/O; the rest
    are constants the firmware build bakes in (geometry + the trusted key set +
    this device's board id + the running firmware's platform version).
    """

    def __init__(self, read, verify, mount, write_marker, partition_size,
                 front_size, block, product_id, trusted_keys, platform_version):
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

    def _rollback_floor(self):
        """The anti-rollback floor for FRONT: the higher of BACK's factory version and the
        monotonic floor in BACK's rollback sector (advanced by confirm() as updates are
        kept). 0 if BACK's trailer doesn't parse (a torn factory image) and no floor logged."""
        back = self.read(self.partition_size - self.block, self.block)
        try:
            base = parse_trailer(back).payload_version
        except OtaReject:
            base = 0
        logged = _rollback_floor_of(self.read(self.partition_size - 3 * self.block, self.block))
        return logged if logged > base else base

    def _try_slot(self, offset, slot_size, is_front, rollback_floor):
        blk = self.block
        body = self.read(offset, slot_size - 2 * blk)
        status = self.read(offset + slot_size - 2 * blk, blk)
        trailer = self.read(offset + slot_size - blk, blk)
        t, write_tried = evaluate_slot(
            body, status, trailer, is_front, rollback_floor, self.product_id,
            self.trusted_keys, self.platform_version, self.verify)
        if write_tried:
            # Arm 'tried' *before* mounting: if the trial image hangs, the next boot
            # sees pending+tried+!confirmed and rejects FRONT, falling back to BACK.
            try:
                self.write_marker(offset + slot_size - 2 * blk + _TRIED_OFF, TRIED)
            except OSError:
                # The arm write failed/can't be verified. Running FRONT now would be an
                # untracked trial -- if it hung, the next boot couldn't tell to recover.
                # So don't trust it; fall back to the golden image instead.
                raise OtaReject("trial-arm")
        self.mount(memoryview(body)[:t.body_size])
        return t

    def run(self):
        """Mount FRONT, else BACK. Returns ``(slot, trailer, front_reason)`` where
        ``front_reason`` is the FRONT rejection reason when BACK was used (else
        None). Raises :class:`OtaReject('no-slot:...')` if neither slot mounts."""
        floor = self._rollback_floor()
        try:
            t = self._try_slot(0, self.front_size, True, floor)
            return "FRONT", t, None
        except OtaReject as front_err:
            try:
                t = self._try_slot(self.front_size,
                                   self.partition_size - self.front_size, False, 0)
                return "BACK", t, str(front_err)
            except OtaReject as back_err:
                raise OtaReject("no-slot:%s/%s" % (front_err, back_err))


# --- Telemetry the app reads after boot completes ---------------------------
# boot.py can't write to UART/REPL (not initialised yet in the frozen boot path), so it
# records the outcome for the app to read once it's running. _main also mirrors these
# onto the _ota_config module (see below) -- that's the channel the app-side openmv_ota
# library actually reads, since importing *this* module would re-run the boot logic.

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
    except ImportError:
        # A core that doesn't build mbedtls can't verify signatures, so it never runs
        # OTA: e.g. the Alif AE3 M55_HE helper core, which is slaved to the main core
        # and has its romfs written by it. Leave mp_init's stock /rom mount in place.
        return

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

    def read(off, size):
        return uctypes.bytearray_at(base + off, size)

    def mount(body):
        vfs.mount(vfs.VfsRom(body), "/rom")

    def write_marker(off, marker):
        # Write, then read back and verify. A rejected (negative rc) or silently failed
        # write raises OSError, which _try_slot turns into a fall-back to BACK -- we
        # never run a trial we couldn't record. The status sector is pre-erased, so the
        # block-device path writes the marker byte-granularly (3-arg writeblocks, no erase).
        if bdev is not None:
            _bs = bdev.ioctl(5, 0)
            bdev.writeblocks(off // _bs, marker, off % _bs)
        elif vfs.rom_ioctl(4, 0, off, marker) < 0:
            raise OSError("rom_ioctl write failed")
        if read(off, len(marker)) != marker:
            raise OSError("marker write verify failed")

    try:
        vfs.umount("/rom")           # drop mp_init's whole-partition auto-mount
    except OSError:
        pass

    try:
        slot, trailer, front_reason = OtaBoot(
            read, verify, mount, write_marker, cfg.PARTITION_SIZE, cfg.FRONT_SIZE,
            cfg.OTA_BLOCK, cfg.PRODUCT_ID, cfg.TRUSTED_KEYS, cfg.PLATFORM_VERSION).run()
    except OtaReject as e:
        if openmv_hilcov is not None:
            openmv_hilcov.mark("boot.no_slot." + str(e).split(":", 1)[0])
        if openmv_log is not None:
            openmv_log.log.error("boot: no bootable slot: %s" % e)
        raise
    if openmv_hilcov is not None:
        openmv_hilcov.mark("boot.mount." + slot)          # FRONT (trial/confirmed) or BACK (golden)
        if front_reason is not None:
            openmv_hilcov.mark("boot.front_reject." + front_reason)
    if openmv_log is not None:
        if front_reason is None:
            openmv_log.log.info("boot: mounted %s (payload %d)" % (slot, trailer.payload_version))
        else:
            openmv_log.log.warning("boot: FRONT rejected (%s) -> mounted %s (payload %d)"
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


try:                                   # the build generates _ota_config beside boot.py
    import _ota_config as _cfg
except ImportError:                    # host / tests: stay inert and importable
    _cfg = None
if _cfg is not None:
    _main(_cfg)  # pragma: no cover

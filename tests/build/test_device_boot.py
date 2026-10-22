"""Host tests for the device ``boot.py`` (``openmv_ota.build.device.boot``).

Fixtures are built with the real host ``ota`` modules, so every trailer is a
genuine ES256-signed trailer in the on-flash format. The injected ``verify``
mirrors the device's ECDSA-over-mbedtls shim by verifying the same raw ``R||S``
signature against the uncompressed public point. Importing the module is inert
(the device ``_main`` only runs when the build-generated ``_ota_config`` is present).
"""

from __future__ import annotations

import hashlib
import struct

import pytest

from openmv_ota.build.device import boot as B
from openmv_ota.ota import certificate as cert_mod
from tests import trust
from openmv_ota.ota import keys, sign
from openmv_ota.ota import rollback as host_rollback
from openmv_ota.ota import status as host_status
from openmv_ota.ota import trailer as host_trailer
from openmv_ota.ota.algorithms import ES256, algorithm_for

BLOCK = 4096
FRONT_SIZE = 5 * BLOCK           # body cap = FRONT_SIZE - 4*BLOCK (rollback/spare/status/trailer)
PARTITION_SIZE = 2 * FRONT_SIZE  # BACK slot is the other half
PRODUCT_ID = 0x1234
PLATFORM = (5 << 24)             # running firmware version code
V1 = (1 << 24)                   # payload_version 1.0.0


def _key():
    priv = keys.generate_private_key(algorithm_for(ES256))
    pub_bytes = bytes.fromhex(keys.public_point_hex(priv.public_key()))
    return priv, pub_bytes


def _verify(alg, pubkey_bytes, sig, msg):
    spec = algorithm_for(alg)
    pub = keys.public_key_from_hex(pubkey_bytes.hex(), spec)
    return sign.verify_region(pub, msg, sig, spec)


def _trailer(priv, key_id, body, *, product_id=PRODUCT_ID, payload_version=V1, publish_seq=0,
             body_size=None, meta=None, cert_priv=None, epoch=0, root_priv=trust.ROOT_PRIV,
             **fields):
    """A v4 trailer for ``body`` signed by ``priv``. ``key_id`` keeps the v1 call sites' shape:
    an id below 0x100 means a FACTORY key, which is now the certificate's kind."""
    kind = cert_mod.KIND_FACTORY if key_id < 0x100 else cert_mod.KIND_OTA
    return trust.trailer(priv, body, kind=kind, epoch=epoch, root_priv=root_priv,
                         cert_priv=cert_priv, meta=meta, product_id=product_id,
                         payload_version=payload_version, publish_seq=publish_seq,
                         body_size=body_size, **fields)


def _status(pending, tried, confirmed):
    return host_status.build_status_sector(BLOCK, pending=pending, tried=tried,
                                           confirmed=confirmed)


def _spend_attempts(status, n):
    """Consume ``n`` attempt markers in a status sector, the way boot.py does on device.

    16 bytes each, not one -- see the constants test. Writing single bytes here would let a
    test pass against a layout the N6 hard faults on, which is exactly what happened."""
    out = bytearray(status)
    for i in range(n):
        off = B._ATTEMPTS_OFF + i * B._ATTEMPT_UNIT
        out[off:off + B._ATTEMPT_UNIT] = B.ATTEMPT
    return bytes(out)


def _slot(body, trailer_bytes, status_sector, slot_size):
    out = bytearray(b"\xff" * slot_size)
    out[0:len(body)] = body
    out[slot_size - 2 * BLOCK:slot_size - 2 * BLOCK + len(status_sector)] = status_sector
    out[slot_size - BLOCK:slot_size - BLOCK + len(trailer_bytes)] = trailer_bytes
    return out


# --- constants are pinned to the host source of truth ----------------------

def test_constants_match_host():
    assert B.MAGIC == host_trailer.MAGIC_ROMFS_APP
    assert B.HEADER_VERSION == host_trailer.HEADER_VERSION
    assert B._HEADER_STRUCT == host_trailer.HEADER_STRUCT
    assert B._HEADER_SIZE == host_trailer.HEADER_SIZE
    assert B._COMP_STRUCT == host_trailer.COMPONENT_STRUCT
    assert (B._COMP_SIZE, B._MAX_COMPONENTS) == (host_trailer.COMPONENT_SIZE,
                                                 host_trailer.MAX_COMPONENTS)
    # The device spells the certificate's pad "7s" (MicroPython's struct has no "x"), so the
    # two structs are compared by size and by what they decode, not by their format strings.
    assert B._CERT_SIZE == cert_mod.SIZE and B._CERT_SIGNED == cert_mod.SIGNED_SIZE
    assert struct.calcsize(B._CERT_STRUCT) == struct.calcsize(cert_mod._STRUCT)
    assert (B._CERT_MAGIC, B._CERT_VERSION) == (cert_mod.MAGIC, cert_mod.VERSION)
    assert (B._KIND_OTA, B._KIND_FACTORY) == (cert_mod.KIND_OTA, cert_mod.KIND_FACTORY)
    from openmv_ota.ota import install_record as R
    assert (B._RECORD_MAGIC, B._RECORD_VERSION, B._RECORD_STRUCT, B._RECORD_FIXED) == (
        R.MAGIC, R.VERSION, R.FIXED_STRUCT, R.FIXED_SIZE)
    assert (B._RECORD_BLOCK, B._REASON_TRIAL) == (R.BLOCK, R.REASON_TRIAL)
    assert (B._FLOOR_OFF, B._EPOCH_OFF, B._FLOOR_ENTRIES) == (
        host_status.FLOOR_OFFSET, host_status.EPOCH_OFFSET, host_status.FLOOR_ENTRIES)
    assert (B.PENDING, B.TRIED, B.CONFIRMED) == (
        host_status.PENDING, host_status.TRIED, host_status.CONFIRMED)
    assert (B._PENDING_OFF, B._TRIED_OFF, B._CONFIRMED_OFF) == (
        host_status.PENDING_OFFSET, host_status.TRIED_OFFSET, host_status.CONFIRMED_OFFSET)
    for alg in (-7, -35, -36):
        assert B._ALG_SIG_SIZE[alg] == algorithm_for(alg).sig_size
    assert B._ROLLBACK_ENTRY == host_rollback.ENTRY_SIZE
    assert (B._COUNTER_OFF, B._COUNTER_LEN) == (
        host_status.COUNTER_OFFSET, host_status.COUNTER_SIZE)
    assert (B._ATTEMPTS_OFF, B._ATTEMPTS_MAX, B._ATTEMPT_UNIT) == (
        host_status.ATTEMPTS_OFFSET, host_status.ATTEMPTS_MAX, host_status.ATTEMPT_UNIT)
    # EVERY flash write this system makes is 8 or 16 bytes. A 1-byte write is not portable --
    # the N6's octal-DTR XSPI hard faults on one -- and the attempt marker was the only odd
    # size in the tree until hardware found it.
    assert B._ATTEMPT_UNIT == B.MARKER_SIZE == 16
    assert B._ATTEMPTS_OFF % B.MARKER_SIZE == 0
    assert len(B.ATTEMPT) == B._ATTEMPT_UNIT
    # ...and the VALUE too, not just the geometry: anything off-device that builds a spent-trial
    # sector (the QEMU suite does) writes host_status.ATTEMPT, so a drift here would make it
    # craft sectors boot.py reads as "attempts remaining" and quietly stop testing the rejection.
    assert B.ATTEMPT == host_status.ATTEMPT


def test_install_counter_reads_the_host_encoding():
    # The builder writes the counter; boot.py reads it. Pin the two together -- a mismatch
    # here would ship a device that cannot tell which of its two slots is newer.
    sector = host_status.build_status_sector(BLOCK, pending=False, tried=False,
                                             confirmed=True, counter=7)
    assert B.install_counter(sector) == 7
    assert B.install_counter(host_status.build_status_sector(
        BLOCK, pending=False, tried=False, confirmed=True)) is None


def test_rollback_floor_of_matches_host():
    from openmv_ota.ota import rollback as host
    sector = bytearray(b"\xff" * BLOCK)
    sector[0:host.ENTRY_SIZE] = host.encode_entry(0x01000000)
    sector[host.ENTRY_SIZE:2 * host.ENTRY_SIZE] = host.encode_entry(0x01020000)
    assert B._rollback_floor_of(bytes(sector)) == host.floor_of(sector) == 0x01020000
    assert B._rollback_floor_of(b"\xff" * BLOCK) == 0          # blank -> no floor


# --- parse_trailer ----------------------------------------------------------

def test_parse_trailer_valid():
    priv, _pub = _key()
    body = b"romfs-body" * 5
    t = B.parse_trailer(_trailer(priv, 0x100, body, payload_version=V1))
    assert t.body_size == len(body) and t.product_id == PRODUCT_ID
    assert t.kind == B._KIND_OTA and t.sig_alg == ES256 and t.payload_version == V1
    assert t.body_sha256 == hashlib.sha256(body).digest()
    assert len(t.signature) == 64


def test_parse_trailer_too_short():
    with pytest.raises(B.OtaReject, match="trunc"):
        B.parse_trailer(b"\x00" * 10)


@pytest.mark.parametrize(("mutate", "reason"), [
    (lambda b: b"XXXX" + b[4:], "magic"),                         # bad magic
    (lambda b: b[:4] + b"\x01" + b[5:], "version"),               # header_version=1 (the 32-bit id)
    (lambda b: b[:12] + b"\x40\x00\x00\x00" + b[16:], "components"),  # comp_count 64 > the bound
    (lambda b: b[:96] + b"XXXX" + b[100:], "cert"),               # certificate magic (checked before the CRC)
    (lambda b: b[:96 + 20] + b"\xdd\xff\xff\xff" + b[120:], "cert"),  # certificate alg -> not ES256
    # past the 96-byte header but short of body_end -- the framing check, not the
    # is-there-even-a-header one above
    (lambda b: b[:120], "trunc"),
])
def test_parse_trailer_malformed(mutate, reason):
    priv, _pub = _key()
    good = _trailer(priv, 0x100, b"abc" * 30)
    with pytest.raises(B.OtaReject, match=reason):
        B.parse_trailer(mutate(good))


def test_parse_trailer_bad_crc():
    priv, _pub = _key()
    good = bytearray(_trailer(priv, 0x100, b"abc" * 30))
    good[-8] ^= 0xFF        # flip a signature byte (inside the CRC'd region)
    with pytest.raises(B.OtaReject, match="crc"):
        B.parse_trailer(bytes(good))


# --- evaluate_slot ----------------------------------------------------------

def _eval(trailer_bytes, body, status, *, is_front=True, floor=0, product_id=PRODUCT_ID,
          roots=None, verify=_verify, max_attempts=3, epoch_floor=0):
    # `is_front` is accepted and ignored: v2 evaluates both slots by the SAME rule, and which one
    # wins is select_slot()'s job. Kept in the signature so the v1 call sites read unchanged.
    del is_front
    return B.evaluate_slot(body, status, trailer_bytes, floor, product_id,
                           roots if roots is not None else {}, verify, max_attempts, epoch_floor)


class _Dev:
    """A fake device: a partition bytearray + recorded mounts / marker writes."""

    def __init__(self, partition):
        self.partition = partition
        self.mounted = []
        self.writes = []

    def read(self, off, size):
        return memoryview(self.partition)[off:off + size]

    def mount(self, body):
        self.mounted.append(bytes(body))

    def write_marker(self, off, marker):
        self.partition[off:off + len(marker)] = marker
        self.writes.append((off, marker))

    def boot(self, roots, *, product_id=PRODUCT_ID):
        return B.OtaBoot(self.read, _verify, self.mount, self.write_marker,
                         PARTITION_SIZE, FRONT_SIZE, BLOCK, product_id, roots).run()


def _partition(front_slot, back_slot):
    p = bytearray()
    p += front_slot
    p += back_slot
    return p


def _front(priv, key_id, body, status, **kw):
    return _slot(body, _trailer(priv, key_id, body, **kw), status, FRONT_SIZE)


def _back(priv, key_id, body, status=None, **kw):
    # Takes a status like _front now: under A/B the second slot is a real, updatable image, not a
    # fixed golden shape. Defaults to the settled shape so v1 call sites still read the same.
    return _slot(body, _trailer(priv, key_id, body, **kw),
                 _status(False, False, True) if status is None else status,
                 PARTITION_SIZE - FRONT_SIZE)


# --- evaluate_slot: the v2 truth table -------------------------------------
# ONE rule for BOTH slots. v1 had two tables here -- FRONT ran the trial state machine, BACK had
# to be "exactly the golden factory shape" -- because BACK *was* the factory image and only FRONT
# was ever written. Under A/B both slots are real, updatable images and either may be the newest,
# so a second table would be a second rule for the same thing. Which slot wins is select_slot()'s
# job, not this function's.
#
# "trial" = mount and consume one attempt; "mount" = an already-settled image; anything else is
# the OtaReject reason. `tried` is no longer consulted -- the attempt region supersedes it.
_V2_MARKERS = [
    # pending, confirmed, attempts, expect
    (False, False, 0, "status"),        # blank / erased: nothing was installed here
    (False, True,  0, "mount"),         # factory-flashed slot: confirmed with no trial
    (True,  True,  0, "mount"),         # post-OTA confirmed
    (True,  True,  2, "mount"),         # confirmed wins even after attempts were spent
    (True,  False, 0, "trial"),         # freshly installed: first attempt
    (True,  False, 2, "trial"),         # mid-trial, attempts remain
    (True,  False, 3, "trial-failed"),  # spent its attempts, never confirmed
    (True,  False, 9, "trial-failed"),  # ...and stays failed
]


@pytest.mark.parametrize(("pending", "confirmed", "attempts", "expect"), _V2_MARKERS)
def test_evaluate_slot_v2_truth_table(pending, confirmed, attempts, expect):
    priv, pub = _key()
    body = b"app" * 40
    status = _spend_attempts(_status(pending, False, confirmed), attempts)
    args = (_trailer(priv, 0x100, body), body, status)
    if expect in ("trial", "mount"):
        t, consume = _eval(*args, roots=trust.ROOTS, max_attempts=3)
        assert consume is (expect == "trial") and t.body_size == len(body)
    else:
        with pytest.raises(B.OtaReject, match=expect):
            _eval(*args, roots=trust.ROOTS, max_attempts=3)


def test_both_slots_obey_the_same_rule():
    """The v1 asymmetry is gone: there is no is_front, and no slot gets a different verdict for
    the same bytes. Pinned because reintroducing a per-slot rule is exactly how A/B would quietly
    become FRONT/BACK again."""
    import inspect

    src = inspect.getsource(B.evaluate_slot)
    assert "is_front" not in src
    assert "back-not-factory" not in src


def test_max_attempts_of_one_is_the_old_one_shot_behaviour():
    """The retry counter is a superset, so the conservative setting stays available per product."""
    priv, pub = _key()
    body = b"app" * 40
    fresh = _status(True, False, False)
    t, consume = _eval(_trailer(priv, 0x100, body), body, fresh,
                       roots=trust.ROOTS, max_attempts=1)
    assert consume is True                       # first boot still gets its one attempt

    spent = _spend_attempts(_status(True, False, False), 1)
    with pytest.raises(B.OtaReject, match="trial-failed"):
        _eval(_trailer(priv, 0x100, body), body, spent, roots=trust.ROOTS, max_attempts=1)


# --- run(): which slot boots ------------------------------------------------
# v1 asked "is FRONT acceptable, else fall back to golden BACK". v2 asks "which of the valid
# slots is newest", and "nothing is valid" means recovery rather than a factory image.

def _counter(status, value):
    status[64:68] = value.to_bytes(4, "little")
    status[68:72] = (value ^ 0xFFFFFFFF).to_bytes(4, "little")
    return status


def _floor(status, key):
    """Seed one anti-rollback floor entry in a status sector."""
    status[B._FLOOR_OFF:B._FLOOR_OFF + 16] = host_rollback.encode_entry(key)
    return status


def test_run_boots_the_newest_confirmed_slot():
    priv, pub = _key()
    a, bdy_b = b"aaa" * 40, b"bbb" * 40
    sa = _counter(bytearray(_status(True, True, True)), 3)
    sb = _counter(bytearray(_status(True, True, True)), 5)
    dev = _Dev(_partition(_front(priv, 0x100, a, sa), _back(priv, 0x100, bdy_b, sb)))
    slot, _t, _r = dev.boot(trust.ROOTS)
    assert slot == "B"                       # higher install counter, not higher version


def test_run_boots_the_older_slot_when_it_is_the_newer_install():
    """Ordering is the counter, not slot position -- A wins when A was installed later."""
    priv, pub = _key()
    a, bdy_b = b"aaa" * 40, b"bbb" * 40
    sa = _counter(bytearray(_status(True, True, True)), 9)
    sb = _counter(bytearray(_status(True, True, True)), 5)
    dev = _Dev(_partition(_front(priv, 0x100, a, sa), _back(priv, 0x100, bdy_b, sb)))
    assert dev.boot(trust.ROOTS)[0] == "A"


def test_run_consumes_an_attempt_for_a_trial_and_mounts_it():
    priv, pub = _key()
    a, bdy_b = b"aaa" * 40, b"bbb" * 40
    sa = _counter(bytearray(_status(True, True, True)), 3)      # settled
    sb = _counter(bytearray(_status(True, False, False)), 4)    # fresh trial, newer
    dev = _Dev(_partition(_front(priv, 0x100, a, sa), _back(priv, 0x100, bdy_b, sb)))
    slot, _t, _r = dev.boot(trust.ROOTS)
    assert slot == "B" and len(dev.writes) == 1                 # one attempt burned


def test_a_spent_trial_falls_back_to_the_OTHER_SLOT_not_a_factory_image():
    """The heart of A/B. v1 fell back to the image the device shipped with -- code nobody had
    run in years. v2 falls back to the last one that worked."""
    priv, pub = _key()
    a, bdy_b = b"aaa" * 40, b"bbb" * 40
    sa = _counter(bytearray(_status(True, True, True)), 3)
    sb = _counter(bytearray(_status(True, False, False)), 4)
    sb = bytearray(_spend_attempts(sb, 3))                                       # attempts exhausted
    dev = _Dev(_partition(_front(priv, 0x100, a, sa), _back(priv, 0x100, bdy_b, sb)))
    slot, _t, reason = dev.boot(trust.ROOTS)
    assert slot == "A" and "trial-failed" in reason


def test_run_raises_no_slot_when_nothing_is_valid():
    """Not a dead end under v2: the caller hands to firmware-resident recovery, which
    re-downloads until a working image exists. The reasons ride along for the log."""
    priv, pub = _key()
    a, bdy_b = b"aaa" * 40, b"bbb" * 40
    blank = bytearray(_status(False, False, False))
    dev = _Dev(_partition(_front(priv, 0x100, a, bytearray(blank)),
                          _back(priv, 0x100, bdy_b, bytearray(blank))))
    with pytest.raises(B.OtaReject, match="no-slot"):
        dev.boot(trust.ROOTS)


def test_an_unwritable_attempt_refuses_the_trial_rather_than_running_it_untracked():
    """If the attempt cannot be recorded the trial cannot be bounded, and an unbounded trial
    that hangs would be retried forever. Refusing hands the boot to the other slot."""
    priv, pub = _key()
    a, bdy_b = b"aaa" * 40, b"bbb" * 40
    sa = _counter(bytearray(_status(True, True, True)), 3)
    sb = _counter(bytearray(_status(True, False, False)), 4)
    dev = _Dev(_partition(_front(priv, 0x100, a, sa), _back(priv, 0x100, bdy_b, sb)))

    def boom(off, marker):
        raise OSError("flash write failed")

    dev.write_marker = boom
    slot, _t, reason = dev.boot(trust.ROOTS)
    assert slot == "A" and "trial-arm" in reason


def test_log_is_a_null_logger_on_host():
    # openmv_log is absent off-device, so boot.py's `log` is a null logger -- boot.py logs
    # unconditionally (no is-not-None guard) and stays inert + importable on the host.
    assert B.log.debug("d") is None
    assert B.log.info("i") is None
    assert B.log.warning("w") is None
    assert B.log.error("e") is None
    assert B.log.critical("c") is None


# --- v2: install counter + attempt region ------------------------------------------------
# Both live in the status sector past the four markers, and both are written WITHOUT erasing:
# the sector is blank after the slot erase and every write from then on only clears bits. That
# is what lets control data share an erase block with the body on the single-sector boards.

def _blank_status():
    return bytearray(b"\xff" * 4096)


def _with_counter(value):
    st = _blank_status()
    st[64:68] = value.to_bytes(4, "little")
    st[68:72] = (value ^ 0xFFFFFFFF).to_bytes(4, "little")
    return st


def test_install_counter_orders_slots_without_consulting_the_version():
    """A/B ordering hangs on this rather than the version, because the version cannot order two
    slots when the SAME version is legitimately installed twice -- a re-install, or a re-flash of
    the same release. Supporting that is the point."""
    assert B.install_counter(_with_counter(0)) == 0
    assert B.install_counter(_with_counter(7)) == 7
    assert B.install_counter(_with_counter(0xFFFFFFFE)) == 0xFFFFFFFE


def test_an_unwritten_counter_is_not_a_counter():
    """Blank flash is 0xFF everywhere, which as a u32 is a very large number. Reading that as
    'newest' would make an erased slot outrank a real install."""
    assert B.install_counter(_blank_status()) is None


def test_a_torn_counter_is_rejected_rather_than_believed():
    """An install that lost power partway must not be able to claim it is the newest slot."""
    st = _with_counter(7)
    st[68] ^= 0x01                                   # corrupt the complement
    assert B.install_counter(st) is None


def test_attempts_append_rather_than_increment():
    """Flash cannot be incremented in place without an erase, and there is none available here:
    the slot is erased once at install and everything after is a 1->0 program. Appending costs
    nothing, and a torn write costs one attempt instead of corrupting a count.

    One 16-BYTE marker per boot, not one byte: 16 is the portable write unit (one AE3 MRAM
    write, and what every other marker uses). A 1-byte program hard faults on the N6's octal
    DTR XSPI -- silently, on the first boot of every trial -- which is how hardware found it."""
    st = _blank_status()
    assert B.attempts_used(st) == 0
    assert B.attempt_offset(st) == B._ATTEMPTS_OFF

    st = bytearray(_spend_attempts(st, 1))
    assert B.attempts_used(st) == 1
    assert B.attempt_offset(st) == B._ATTEMPTS_OFF + B._ATTEMPT_UNIT

    st = bytearray(_spend_attempts(st, 2))
    assert B.attempts_used(st) == 2


def test_the_attempt_region_is_bounded():
    """A slot that has needed 64 boots is not coming back; the region must not run off the end
    of the status sector into whatever follows."""
    st = _blank_status()
    st = bytearray(_spend_attempts(st, 64))
    assert B.attempts_used(st) == 64
    assert B.attempt_offset(st) is None


# --- v2: which slot boots ----------------------------------------------------------------

def test_newest_install_counter_wins():
    assert B.select_slot([("A", 3, True), ("B", 5, False)]) == "B"
    assert B.select_slot([("A", 9, False), ("B", 5, True)]) == "A"


def test_nothing_bootable_means_recovery():
    """The v1 answer was 'fall back to the factory image'. There isn't one any more, so the
    honest answer is None and the caller hands to the firmware-resident OTA flow."""
    assert B.select_slot([]) is None


def test_a_slot_with_no_readable_counter_still_boots_but_sorts_last():
    """It must remain bootable -- the first boot after a factory flash has no counter anywhere --
    but it cannot outrank a slot that actually claims to be newer."""
    assert B.select_slot([("A", None, True), ("B", 0, False)]) == "B"
    assert B.select_slot([("A", None, True)]) == "A"


def test_ties_prefer_a_confirmed_slot():
    """Ties happen for exactly two reasons -- a factory flash that wrote both slots, and
    corruption -- so they get a defined answer rather than input order. A CONFIRMED slot is known
    to have run, which is the better bet."""
    assert B.select_slot([("A", 4, False), ("B", 4, True)]) == "B"
    assert B.select_slot([("A", None, False), ("B", None, True)]) == "B"


def test_ordering_never_consults_the_version():
    """The whole reason for a counter: two slots holding the SAME version is the case this
    supports, so version can play no part in choosing between them."""
    import inspect

    src = inspect.getsource(B.select_slot)
    assert "version" not in src.split('"""')[2], "select_slot must not read a version"


def test_a_truncated_status_sector_yields_no_counter():
    """Defensive: a short read must not be parsed as a counter. Anything that produces a number
    from garbage here would let a damaged slot claim to be the newest."""
    assert B.install_counter(bytearray(b"\xff" * 4)) is None


def test_a_real_counter_outranks_an_unreadable_one_in_either_order():
    """Pins both directions of the None comparison, so the result cannot depend on which slot
    the caller happened to list first."""
    assert B.select_slot([("A", 2, False), ("B", None, True)]) == "A"
    assert B.select_slot([("A", None, True), ("B", 2, False)]) == "B"


# --- evaluate_slot: every rejection reason ------------------------------------------------
# The signature is checked BEFORE any header field is trusted, so these split in two: `key` and
# `sig` are refusals to believe the header at all, the rest are verdicts on a header already
# proven authentic.

def test_evaluate_rejects_a_certificate_from_a_root_this_firmware_does_not_embed():
    priv, _pub = _key()
    body = b"app" * 40
    with pytest.raises(B.OtaReject, match="root"):
        _eval(_trailer(priv, 0x100, body), body, _status(True, True, True), roots={})


def test_evaluate_rejects_a_certificate_whose_root_signature_is_wrong():
    """The right root id, signed by a different key: the trust chain breaks at the top."""
    priv, _pub = _key()
    other_root, _ = _key()
    body = b"app" * 40
    with pytest.raises(B.OtaReject, match="cert-sig"):
        _eval(_trailer(priv, 0x100, body, root_priv=other_root), body, _status(True, True, True),
              roots=trust.ROOTS)


def test_evaluate_rejects_a_retired_key_by_epoch():
    priv, _pub = _key()
    body = b"app" * 40
    with pytest.raises(B.OtaReject, match="epoch"):
        _eval(_trailer(priv, 0x100, body, epoch=2), body, _status(True, True, True),
              roots=trust.ROOTS, epoch_floor=3)
    t, _c = _eval(_trailer(priv, 0x100, body, epoch=3), body, _status(True, True, True),
                  roots=trust.ROOTS, epoch_floor=3)        # equal is fine
    assert t.epoch == 3


def test_evaluate_rejects_a_bad_signature():
    """A genuine certificate for one key, an image signature made with another."""
    priv, _pub = _key()
    priv2, _pub2 = _key()
    body = b"app" * 40
    with pytest.raises(B.OtaReject, match="sig"):
        _eval(_trailer(priv, 0x100, body, cert_priv=priv2), body, _status(True, True, True),
              roots=trust.ROOTS)


@pytest.mark.parametrize(("kw", "reason"), [
    ({"product_id": 0x999}, "board"),               # cross-flash guard
])
def test_evaluate_rejects_an_authentic_header_that_does_not_fit_this_device(kw, reason):
    priv, pub = _key()
    body = b"app" * 40
    with pytest.raises(B.OtaReject, match=reason):
        _eval(_trailer(priv, 0x100, body, **kw), body, _status(True, True, True),
              roots=trust.ROOTS)


def test_evaluate_rejects_a_body_bigger_than_the_slot():
    priv, pub = _key()
    body = b"app" * 40
    with pytest.raises(B.OtaReject, match="size"):
        _eval(_trailer(priv, 0x100, body), body[:8], _status(True, True, True),
              roots=trust.ROOTS)


def test_evaluate_rejects_a_body_whose_hash_does_not_match():
    priv, pub = _key()
    body = b"app" * 40
    with pytest.raises(B.OtaReject, match="body-sha"):
        _eval(_trailer(priv, 0x100, body), b"x" + body[1:], _status(True, True, True),
              roots=trust.ROOTS)


def test_evaluate_rejects_an_UNPROVEN_version_below_the_rollback_floor():
    """Anti-rollback gates what may be PROMOTED: a slot that has not been confirmed and is
    below the floor is a replayed old release and must not run."""
    priv, pub = _key()
    body = b"app" * 40
    with pytest.raises(B.OtaReject, match="rollback"):
        _eval(_trailer(priv, 0x100, body, payload_version=2), body, _status(True, False, False),
              roots=trust.ROOTS, floor=99)


def test_a_CONFIRMED_slot_below_the_floor_is_still_bootable():
    """The fallback must survive its own success. The floor rises to the running version on
    every confirm, so the slot behind an accepted update is below the floor BY CONSTRUCTION --
    rejecting it there deletes the safety net at the moment the device finished proving it did
    not need it, leaving it one bad update from having nothing to return to.

    Found on hardware: a Nicla that confirmed 1.1.0 then logged `boot: rejected A:rollback`.
    The floor is enforced where it belongs -- pre-erase in the installer, and above on any slot
    not yet confirmed. An attacker who can force a trial to fail can force this downgrade
    anyway; the plan says so outright, as inherent to A/B."""
    priv, pub = _key()
    body = b"app" * 40
    t, consume = _eval(_trailer(priv, 0x100, body, payload_version=2), body,
                       _status(True, True, True), roots=trust.ROOTS, floor=99)
    assert consume is False and t.payload_version == 2


def test_single_mode_presents_one_slot_spanning_the_partition():
    """SINGLE and A/B differ in exactly one place -- the slot list -- so the rest of run() is
    written once. front_size of 0 (or the whole partition) means there is nothing to split."""
    ob = B.OtaBoot(None, None, None, None, 131072, 0, 4096, 0, {})
    assert ob._slots() == [("A", 0, 131072)]
    ob2 = B.OtaBoot(None, None, None, None, 131072, 131072, 4096, 0, {})
    assert ob2._slots() == [("A", 0, 131072)]


def test_the_rollback_floor_is_the_max_across_slots():
    """v1 read it from BACK alone, which worked because BACK was never erased. Under A/B every
    slot is erased in turn, so a floor living in one slot would vanish when that slot is
    rewritten -- the max is what keeps it monotonic."""
    part = bytearray(b"\xff" * PARTITION_SIZE)
    fo = B._FLOOR_OFF
    part[FRONT_SIZE - 2 * BLOCK + fo:FRONT_SIZE - 2 * BLOCK + fo + 8] = \
        host_rollback.encode_entry(4)
    part[PARTITION_SIZE - 2 * BLOCK + fo:PARTITION_SIZE - 2 * BLOCK + fo + 8] = \
        host_rollback.encode_entry(11)
    dev = _Dev(part)
    ob = B.OtaBoot(dev.read, _verify, dev.mount, dev.write_marker,
                   PARTITION_SIZE, FRONT_SIZE, BLOCK, PRODUCT_ID, {})
    assert ob._rollback_floor() == 11


def test_the_epoch_floor_is_the_max_across_slots_and_stays_out_of_the_rollback_floor():
    part = bytearray(b"\xff" * PARTITION_SIZE)
    fo, eo = B._FLOOR_OFF, B._EPOCH_OFF
    part[FRONT_SIZE - 2 * BLOCK + eo:FRONT_SIZE - 2 * BLOCK + eo + 16] = \
        host_rollback.encode_entry(2)
    part[PARTITION_SIZE - 2 * BLOCK + eo:PARTITION_SIZE - 2 * BLOCK + eo + 16] = \
        host_rollback.encode_entry(5)
    part[PARTITION_SIZE - 2 * BLOCK + fo:PARTITION_SIZE - 2 * BLOCK + fo + 16] = \
        host_rollback.encode_entry(7)
    dev = _Dev(part)
    ob = B.OtaBoot(dev.read, _verify, dev.mount, dev.write_marker,
                   PARTITION_SIZE, FRONT_SIZE, BLOCK, PRODUCT_ID, {})
    assert ob._epoch_floor() == 5
    assert ob._rollback_floor() == 7              # the epoch entries are not versions


def test_a_slot_signed_under_a_retired_epoch_is_rejected_at_boot():
    """confirm() raised the epoch floor to 2 when a release certified at epoch 2 was kept; a
    slot whose certificate says epoch 1 is signed by a key retired since, however new it is."""
    priv, _pub = _key()
    a, bdy_b = b"aaa" * 40, b"bbb" * 40
    sa = _counter(bytearray(_status(True, True, True)), 1)
    eo = B._EPOCH_OFF
    sa[eo:eo + 16] = host_rollback.encode_entry(2)                 # A was confirmed at epoch 2
    sb = _counter(bytearray(_status(True, False, False)), 2)       # B: newer, pending, epoch 1
    dev = _Dev(_partition(_front(priv, 0x100, a, sa, epoch=2), _back(priv, 0x100, bdy_b, sb, epoch=1)))
    slot, _t, reason = dev.boot(trust.ROOTS)
    assert slot == "A" and "B:epoch" in reason
    # ...while a slot at (or above) the floor is fine
    sb2 = _counter(bytearray(_status(True, False, False)), 2)
    dev = _Dev(_partition(_front(priv, 0x100, a, sa, epoch=2), _back(priv, 0x100, bdy_b, sb2, epoch=2)))
    assert dev.boot(trust.ROOTS)[0] == "B"


def test_a_slot_whose_attempt_region_is_full_is_not_bootable():
    """Belt-and-braces against the two limits disagreeing. max_attempts (3) normally rejects the
    trial long before the 64-byte region fills, so reaching this means someone raised max_attempts
    past the region -- and running an unrecordable trial is exactly what the region prevents."""
    priv, pub = _key()
    a, bdy_b = b"aaa" * 40, b"bbb" * 40
    sa = _counter(bytearray(_status(True, True, True)), 3)
    sb = _counter(bytearray(_status(True, False, False)), 4)
    sb = bytearray(_spend_attempts(sb, 64))                      # region full, but max_attempts is higher still
    dev = _Dev(_partition(_front(priv, 0x100, a, sa), _back(priv, 0x100, bdy_b, sb)))
    ob = B.OtaBoot(dev.read, _verify, dev.mount, dev.write_marker, PARTITION_SIZE,
                   FRONT_SIZE, BLOCK, PRODUCT_ID, trust.ROOTS, max_attempts=999)
    slot, _t, reason = ob.run()
    assert slot == "A" and "trial-attempts-full" in reason


# --- the boot stub's install record ------------------------------------------------------

def _record(slot, components, *, stub_version=7, reason=1, raw=None):
    from openmv_ota.ota import install_record as R
    out = raw if raw is not None else R.pack(R.InstallRecord(stub_version, slot, reason, components))
    return out + b"\xff" * (4096 - len(out))              # the rest of the record sector


def test_parse_record_mirrors_the_host_and_ignores_anything_else():
    from openmv_ota.ota import install_record as R
    good = _record(1, [(0x70090000, 200, b"\x11" * 32)])
    assert B.parse_record(good) == (7, 1, 1, [(0x70090000, 200, b"\x11" * 32)])
    assert B.parse_record(None) is None and B.parse_record(b"\xff" * 4096) is None   # erased
    assert B.parse_record(b"\xff" * 10) is None
    assert B.parse_record(b"OMVX" + good[4:]) is None                           # magic
    assert B.parse_record(good[:4] + b"\x02\x00\x00\x00" + good[8:]) is None   # version
    assert B.parse_record(good[:16] + b"\x40\x00\x00\x00" + good[20:]) is None  # comp_count
    assert B.parse_record(good[:12] + b"\x05\x00\x00\x00" + good[16:]) is None  # slot
    assert B.parse_record(good[:32 + 40 + 2]) is None                            # truncated
    bad = bytearray(good)
    bad[40] ^= 0x01                                                             # a hash byte
    assert B.parse_record(bytes(bad)) is None                                   # crc
    assert B.parse_record(_record(0, [], reason=R.REASON_REVERTED)) == (7, 0, 2, [])


def _comp(fill):
    from openmv_ota.ota.trailer import Component
    # the record names components by installed hash; the blob hash differs on purpose so a
    # match on the wrong field cannot pass
    return Component(0x70090000, 4096, 100, 200, bytes([fill ^ 0xFF]) * 32, bytes([fill]) * 32)


def test_the_stub_record_picks_the_slot_and_burns_no_attempt():
    """The stub installed B's firmware and burned B's attempt; boot.py mounts B's filesystem
    without spending another, whatever the install counters would have said."""
    priv, _pub = _key()
    a, bdy_b = b"aaa" * 40, b"bbb" * 40
    sa = _counter(bytearray(_status(True, True, True)), 9)        # A: confirmed, newest
    sb = _counter(bytearray(_status(True, False, False)), 1)      # B: pending, older
    dev = _Dev(_partition(_front(priv, 0x100, a, sa, components=[_comp(0xAA)]),
                          _back(priv, 0x100, bdy_b, sb, components=[_comp(0xBB)])))
    ob = B.OtaBoot(dev.read, _verify, dev.mount, dev.write_marker, PARTITION_SIZE, FRONT_SIZE,
                   BLOCK, PRODUCT_ID, trust.ROOTS,
                   record=_record(1, [(0x70090000, 200, bytes([0xBB]) * 32)]))
    slot, t, reason = ob.run()
    assert slot == "B" and dev.mounted == [bdy_b] and reason is None
    assert dev.writes == [] and ob.stub == (7, 1)             # no attempt burned by boot.py


@pytest.mark.parametrize("mark", ["trial-failed", "status", "rollback"])
def test_the_stub_record_overrides_this_firmwares_mark_policy(mark):
    """The stub installed B and burned B's attempt under ITS budget. This firmware was built
    with a smaller one (or sees B as blank, or below its floor): mounting A's app over B's
    firmware would be the one thing the record exists to prevent, so B is mounted anyway. Only
    a verification failure (signature, identity) refuses a record."""
    priv, _pub = _key()
    a, bdy_b = b"aaa" * 40, b"bbb" * 40
    sa = _counter(bytearray(_status(True, True, True)), 9)
    if mark == "trial-failed":
        sb = _spend_attempts(_counter(bytearray(_status(True, False, False)), 1), B.DEFAULT_MAX_ATTEMPTS)
    elif mark == "status":
        sb = _counter(bytearray(_status(False, False, False)), 1)     # never marked at all
    else:
        sb = _counter(bytearray(_status(True, False, False)), 1)
        sa = _floor(sa, 0x7FFFFFFF)                                   # A's floor is above B
    dev = _Dev(_partition(_front(priv, 0x100, a, sa, components=[_comp(0xAA)]),
                          _back(priv, 0x100, bdy_b, sb, components=[_comp(0xBB)])))
    ob = B.OtaBoot(dev.read, _verify, dev.mount, dev.write_marker, PARTITION_SIZE, FRONT_SIZE,
                   BLOCK, PRODUCT_ID, trust.ROOTS,
                   record=_record(1, [(0x70090000, 200, bytes([0xBB]) * 32)]))
    slot, _t, reason = ob.run()
    assert slot == "B" and dev.mounted == [bdy_b] and reason is None   # B's own reject dropped
    assert dev.writes == [] and ob.stub == (7, 1)
    assert B.OtaBoot(dev.read, _verify, dev.mount, dev.write_marker, PARTITION_SIZE, FRONT_SIZE,
                     BLOCK, PRODUCT_ID, trust.ROOTS).run()[0] == "A"    # no record: policy rules


@pytest.mark.parametrize("record", [
    None,                                                     # no stub
    b"\xff" * 4096,                                           # erased sector
    "stale",                                                  # names B, but not B's components
    "rejected",                                               # names a slot that does not verify
])
def test_without_a_usable_record_the_slot_choice_runs_as_usual(record):
    priv, _pub = _key()
    a, bdy_b = b"aaa" * 40, b"bbb" * 40
    sa = _counter(bytearray(_status(True, True, True)), 9)
    sb = _counter(bytearray(_status(True, False, False)), 1)
    back = _back(priv, 0x100, bdy_b, sb, components=[_comp(0xBB)])
    if record == "stale":
        record = _record(1, [(0x70090000, 200, bytes([0xCC]) * 32)])
    elif record == "rejected":
        record = _record(1, [(0x70090000, 200, bytes([0xBB]) * 32)])
        back = _back(priv, 0x100, bdy_b, sb, components=[_comp(0xBB)], product_id=0x999)
    dev = _Dev(_partition(_front(priv, 0x100, a, sa, components=[_comp(0xAA)]), back))
    ob = B.OtaBoot(dev.read, _verify, dev.mount, dev.write_marker, PARTITION_SIZE, FRONT_SIZE,
                   BLOCK, PRODUCT_ID, trust.ROOTS, record=record)
    slot, _t, _reason = ob.run()
    assert slot == "A" and dev.mounted == [a] and ob.stub is None


# --- which number a camera orders by ----------------------------------------

def test_an_ordinary_camera_orders_by_payload_version():
    """It can never be offered another product's image -- the cross-flash guard sees to
    that -- so its own product's version sequence is a complete ordering, and it is the
    one such a camera has always used. `publish_seq` is not consulted at all."""
    priv, pub = _key()
    body = b"app" * 8
    t = B.parse_trailer(_trailer(priv, 0x100, body, payload_version=V1, publish_seq=0))
    assert B.rollback_key(t, PRODUCT_ID) == V1

    # below the floor by version -> refused, whatever publish_seq says
    with pytest.raises(B.OtaReject, match="rollback"):
        _eval(_trailer(priv, 0x100, body, payload_version=V1, publish_seq=1 << 40),
              body, _status(True, False, False), roots=trust.ROOTS, floor=V1 + 1)


def test_a_stock_camera_orders_by_publish_seq():
    """PRODUCT_ID 0 turns the cross-flash guard off, which is how stock hardware becomes
    some customer's unit after it ships. Such a camera CAN change product, and two
    products' version numbers say nothing about each other -- so it orders by the
    account's publish counter, which spans them."""
    priv, pub = _key()
    body = b"app" * 8
    t = B.parse_trailer(_trailer(priv, 0x100, body, product_id=7,
                                 payload_version=V1, publish_seq=500))
    assert B.rollback_key(t, 0) == 500                 # the seq, not the version

    # a high version does NOT clear a floor set by the counter
    with pytest.raises(B.OtaReject, match="rollback"):
        _eval(_trailer(priv, 0x100, body, product_id=7,
                       payload_version=9 << 24, publish_seq=499),
              body, _status(True, False, False), product_id=0,
              roots=trust.ROOTS, floor=500)

    # ...and a LOW version clears it when the counter has advanced, which is the whole
    # point: returning a claimed camera to a stock image numbered 1.0.0
    t2, _consume = _eval(_trailer(priv, 0x100, body, product_id=0,
                                  payload_version=1 << 24, publish_seq=501),
                         body, _status(True, False, False), product_id=0,
                         roots=trust.ROOTS, floor=500)
    assert t2.publish_seq == 501


def test_a_stock_camera_with_no_counter_has_no_floor_to_clear():
    """A project that never opted in stamps 0, and a camera whose floor is 0 takes it --
    so an operator who forgets is not silently locked out on the first install; it is the
    SECOND that refuses, which is the failure the server catches at publish."""
    priv, pub = _key()
    body = b"app" * 8
    t, _c = _eval(_trailer(priv, 0x100, body, product_id=7, publish_seq=0),
                  body, _status(True, False, False), product_id=0,
                  roots=trust.ROOTS, floor=0)
    assert t.publish_seq == 0


# --- the camera's keys, on a factory image's boot --------------------------------------------

class _Console:
    """The console as boot.py's key window sees it: bytes arrive over time (``feed``), ``poll``
    advances the clock while it waits."""

    def __init__(self, text=""):
        self.buf = list(text)
        self.now = 0

    def read(self, n):
        return self.buf.pop(0)

    def poll(self, ms):
        if self.buf:
            return True
        self.now += ms                          # nothing came: the wait used the time
        return False

    def elapsed(self):
        return self.now


CH = bytes(range(32))


def test_the_key_window_returns_the_tool_s_challenge():
    con = _Console("noise from the IDE\n" + B._KEYS_REQUEST + CH.hex() + "\r\n")
    assert B.keys_challenge(con, con.poll, con.elapsed) == CH


@pytest.mark.parametrize("text", [
    "",                                                     # nobody asked
    B._KEYS_REQUEST + CH.hex()[:-2] + "\n",                 # 31 bytes
    B._KEYS_REQUEST + "zz" * 32 + "\n",                     # not hex
    B._KEYS_REQUEST + CH.hex(),                             # never finished
    "x" * 500 + "\n",                                       # a runaway line stays bounded
])
def test_the_key_window_ignores_anything_else_and_closes(text):
    con = _Console(text)
    assert B.keys_challenge(con, con.poll, con.elapsed, window_ms=2000) is None
    assert con.now >= 2000 or text


class _Keys:
    def public_key(self):
        return b"\x04" + bytes(64)

    def ecdh_public_key(self):
        return b"\x04" + b"\x01" * 64

    def sign(self, digest):
        assert digest == CH
        return b"\x30\x06\x02\x01\x01\x02\x01\x01"


class _Se:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, 0

    def provision(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


def test_a_factory_boot_makes_the_keys_and_answers_the_tool():
    se = _Se((_Keys(), True))
    line = B.factory_keys(B._KIND_FACTORY, se, CH, "0036324E4236")
    assert line == "SE-ID %s %s %s 1 0036324E4236" % ("04" + "00" * 64, "04" + "01" * 64,
                                                      "3006020101020101")
    assert B.factory_keys(B._KIND_FACTORY, se, CH).endswith(" 1 -")              # couldn't name itself
    assert B.factory_keys(B._KIND_FACTORY, _Se((_Keys(), False)), None) == "SE-OK"   # nobody asked


def test_only_a_factory_image_with_the_keys_code_makes_keys():
    se = _Se((_Keys(), True))
    assert B.factory_keys(B._KIND_OTA, se, CH) is None              # an OTA image
    assert se.calls == 0
    assert B.factory_keys(B._KIND_FACTORY, None, CH) is None                         # no openmv_ota.se
    assert B.factory_keys(B._KIND_FACTORY, _Se(None), CH) == "SE-NONE"               # a board with none


def test_damaged_keys_are_reported_and_never_stop_the_boot():
    se = _Se(error=OSError("key store: the camera's newest key record is damaged"))
    assert B.factory_keys(B._KIND_FACTORY, se, CH) == ("SE-ERROR key store: the camera's newest key "
                                              "record is damaged")


def test_the_key_window_closes_early_when_no_host_has_the_console_open():
    """stm32: no DTR past the grace -> nobody is flashing; don't hold the app back."""
    con = _Console()
    assert B.keys_challenge(con, con.poll, con.elapsed, attended=lambda: False) is None
    assert B._KEYS_GRACE_MS <= con.now < B._KEYS_GRACE_MS + B._KEYS_STEP_MS


def test_the_key_window_waits_for_a_host_that_has_the_console_open():
    con = _Console()
    assert B.keys_challenge(con, con.poll, con.elapsed, attended=lambda: True) is None
    assert con.now >= B._KEYS_WINDOW_MS


def test_the_key_window_answers_a_host_that_opened_the_console():
    con = _Console(B._KEYS_REQUEST + CH.hex() + "\n")
    assert B.keys_challenge(con, con.poll, con.elapsed, attended=lambda: True) == CH

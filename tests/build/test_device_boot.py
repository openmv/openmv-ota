"""Host tests for the device ``boot.py`` (``openmv_ota.build.device.boot``).

Fixtures are built with the real host ``ota`` modules, so every trailer is a
genuine ES256-signed trailer in the on-flash format. The injected ``verify``
mirrors the device's ECDSA-over-mbedtls shim by verifying the same raw ``R||S``
signature against the uncompressed public point. Importing the module is inert
(the device ``_main`` only runs when the build-generated ``_ota_config`` is present).
"""

from __future__ import annotations

import hashlib

import pytest

from openmv_ota.build.device import boot as B
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


def _trailer(priv, key_id, body, *, product_id=PRODUCT_ID, min_platform=0,
             payload_version=V1, floor=0, body_size=None, alg=ES256, meta=None):
    spec = algorithm_for(alg)
    t = host_trailer.Trailer(
        body_size=len(body) if body_size is None else body_size,
        pad_size=0, meta=meta if meta is not None else {"k": 1},
        product_id=product_id, min_platform_version=min_platform,
        payload_version=payload_version, payload_version_floor=floor,
        key_id=key_id, sig_alg=alg, body_sha256=hashlib.sha256(body).digest())
    t.signature = sign.sign_region(priv, host_trailer.signed_region(t), spec)
    return host_trailer.pack_trailer(t)


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
    assert t.key_id == 0x100 and t.sig_alg == ES256 and t.payload_version == V1
    assert t.body_sha256 == hashlib.sha256(body).digest()
    assert len(t.signature) == 64


def test_parse_trailer_too_short():
    with pytest.raises(B.OtaReject, match="trunc"):
        B.parse_trailer(b"\x00" * 10)


@pytest.mark.parametrize(("mutate", "reason"), [
    (lambda b: b"XXXX" + b[4:], "magic"),                         # bad magic
    (lambda b: b[:4] + b"\x02" + b[5:], "version"),               # header_version=2
    (lambda b: b[:44] + b"\x00\x00\x00\x00" + b[48:], "alg"),     # sig_alg -> 0 (unknown)
    (lambda b: b[:20] + b"\x20\x00\x00\x00" + b[24:], "alg"),     # sig_size -> 32 (!=64)
    (lambda b: b[:90], "trunc"),                                  # chop below body_end
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
          trusted=None, platform=PLATFORM, verify=_verify, max_attempts=3):
    # `is_front` is accepted and ignored: v2 evaluates both slots by the SAME rule, and which one
    # wins is select_slot()'s job. Kept in the signature so the v1 call sites read unchanged.
    del is_front
    return B.evaluate_slot(body, status, trailer_bytes, floor, product_id,
                           trusted if trusted is not None else {}, platform, verify,
                           max_attempts)


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

    def boot(self, trusted, *, product_id=PRODUCT_ID, platform=PLATFORM):
        return B.OtaBoot(self.read, _verify, self.mount, self.write_marker,
                         PARTITION_SIZE, FRONT_SIZE, BLOCK, product_id, trusted,
                         platform).run()


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
        t, consume = _eval(*args, trusted={0x100: pub}, max_attempts=3)
        assert consume is (expect == "trial") and t.body_size == len(body)
    else:
        with pytest.raises(B.OtaReject, match=expect):
            _eval(*args, trusted={0x100: pub}, max_attempts=3)


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
                       trusted={0x100: pub}, max_attempts=1)
    assert consume is True                       # first boot still gets its one attempt

    spent = _spend_attempts(_status(True, False, False), 1)
    with pytest.raises(B.OtaReject, match="trial-failed"):
        _eval(_trailer(priv, 0x100, body), body, spent, trusted={0x100: pub}, max_attempts=1)


# --- run(): which slot boots ------------------------------------------------
# v1 asked "is FRONT acceptable, else fall back to golden BACK". v2 asks "which of the valid
# slots is newest", and "nothing is valid" means recovery rather than a factory image.

def _counter(status, value):
    status[64:68] = value.to_bytes(4, "little")
    status[68:72] = (value ^ 0xFFFFFFFF).to_bytes(4, "little")
    return status


def test_run_boots_the_newest_confirmed_slot():
    priv, pub = _key()
    a, bdy_b = b"aaa" * 40, b"bbb" * 40
    sa = _counter(bytearray(_status(True, True, True)), 3)
    sb = _counter(bytearray(_status(True, True, True)), 5)
    dev = _Dev(_partition(_front(priv, 0x100, a, sa), _back(priv, 0x100, bdy_b, sb)))
    slot, _t, _r = dev.boot({0x100: pub})
    assert slot == "B"                       # higher install counter, not higher version


def test_run_boots_the_older_slot_when_it_is_the_newer_install():
    """Ordering is the counter, not slot position -- A wins when A was installed later."""
    priv, pub = _key()
    a, bdy_b = b"aaa" * 40, b"bbb" * 40
    sa = _counter(bytearray(_status(True, True, True)), 9)
    sb = _counter(bytearray(_status(True, True, True)), 5)
    dev = _Dev(_partition(_front(priv, 0x100, a, sa), _back(priv, 0x100, bdy_b, sb)))
    assert dev.boot({0x100: pub})[0] == "A"


def test_run_consumes_an_attempt_for_a_trial_and_mounts_it():
    priv, pub = _key()
    a, bdy_b = b"aaa" * 40, b"bbb" * 40
    sa = _counter(bytearray(_status(True, True, True)), 3)      # settled
    sb = _counter(bytearray(_status(True, False, False)), 4)    # fresh trial, newer
    dev = _Dev(_partition(_front(priv, 0x100, a, sa), _back(priv, 0x100, bdy_b, sb)))
    slot, _t, _r = dev.boot({0x100: pub})
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
    slot, _t, reason = dev.boot({0x100: pub})
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
        dev.boot({0x100: pub})


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
    slot, _t, reason = dev.boot({0x100: pub})
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

def test_evaluate_rejects_an_unknown_or_revoked_key():
    priv, _pub = _key()
    body = b"app" * 40
    with pytest.raises(B.OtaReject, match="key"):
        _eval(_trailer(priv, 0x100, body), body, _status(True, True, True), trusted={})


def test_evaluate_rejects_a_bad_signature():
    priv, _pub = _key()
    _priv2, pub2 = _key()
    body = b"app" * 40
    with pytest.raises(B.OtaReject, match="sig"):   # signed by priv, trusted key is pub2
        _eval(_trailer(priv, 0x100, body), body, _status(True, True, True), trusted={0x100: pub2})


@pytest.mark.parametrize(("kw", "reason"), [
    ({"product_id": 0x999}, "board"),               # cross-flash guard
    ({"min_platform": PLATFORM + 1}, "compat"),
])
def test_evaluate_rejects_an_authentic_header_that_does_not_fit_this_device(kw, reason):
    priv, pub = _key()
    body = b"app" * 40
    with pytest.raises(B.OtaReject, match=reason):
        _eval(_trailer(priv, 0x100, body, **kw), body, _status(True, True, True),
              trusted={0x100: pub})


def test_evaluate_rejects_a_body_bigger_than_the_slot():
    priv, pub = _key()
    body = b"app" * 40
    with pytest.raises(B.OtaReject, match="size"):
        _eval(_trailer(priv, 0x100, body), body[:8], _status(True, True, True),
              trusted={0x100: pub})


def test_evaluate_rejects_a_body_whose_hash_does_not_match():
    priv, pub = _key()
    body = b"app" * 40
    with pytest.raises(B.OtaReject, match="body-sha"):
        _eval(_trailer(priv, 0x100, body), b"x" + body[1:], _status(True, True, True),
              trusted={0x100: pub})


def test_evaluate_rejects_an_UNPROVEN_version_below_the_rollback_floor():
    """Anti-rollback gates what may be PROMOTED: a slot that has not been confirmed and is
    below the floor is a replayed old release and must not run."""
    priv, pub = _key()
    body = b"app" * 40
    with pytest.raises(B.OtaReject, match="rollback"):
        _eval(_trailer(priv, 0x100, body, payload_version=2), body, _status(True, False, False),
              trusted={0x100: pub}, floor=99)


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
                       _status(True, True, True), trusted={0x100: pub}, floor=99)
    assert consume is False and t.payload_version == 2


def test_single_mode_presents_one_slot_spanning_the_partition():
    """SINGLE and A/B differ in exactly one place -- the slot list -- so the rest of run() is
    written once. front_size of 0 (or the whole partition) means there is nothing to split."""
    ob = B.OtaBoot(None, None, None, None, 131072, 0, 4096, 0, {}, 0)
    assert ob._slots() == [("A", 0, 131072)]
    ob2 = B.OtaBoot(None, None, None, None, 131072, 131072, 4096, 0, {}, 0)
    assert ob2._slots() == [("A", 0, 131072)]


def test_the_rollback_floor_is_the_max_across_slots():
    """v1 read it from BACK alone, which worked because BACK was never erased. Under A/B every
    slot is erased in turn, so a floor living in one slot would vanish when that slot is
    rewritten -- the max is what keeps it monotonic."""
    part = bytearray(b"\xff" * PARTITION_SIZE)
    part[FRONT_SIZE - 3 * BLOCK:FRONT_SIZE - 3 * BLOCK + 8] = host_rollback.encode_entry(4)
    part[PARTITION_SIZE - 3 * BLOCK:PARTITION_SIZE - 3 * BLOCK + 8] = host_rollback.encode_entry(11)
    dev = _Dev(part)
    ob = B.OtaBoot(dev.read, _verify, dev.mount, dev.write_marker,
                   PARTITION_SIZE, FRONT_SIZE, BLOCK, PRODUCT_ID, {}, PLATFORM)
    assert ob._rollback_floor() == 11


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
                   FRONT_SIZE, BLOCK, PRODUCT_ID, {0x100: pub}, PLATFORM, max_attempts=999)
    slot, _t, reason = ob.run()
    assert slot == "A" and "trial-attempts-full" in reason

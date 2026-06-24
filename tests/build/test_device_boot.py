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
from openmv_ota.ota import status as host_status
from openmv_ota.ota import trailer as host_trailer
from openmv_ota.ota.algorithms import ES256, algorithm_for

BLOCK = 4096
FRONT_SIZE = 3 * BLOCK           # body capacity = FRONT_SIZE - 2*BLOCK = 4096
PARTITION_SIZE = 2 * FRONT_SIZE  # BACK slot is the other half
BOARD_ID = 0x1234
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


def _trailer(priv, key_id, body, *, board_id=BOARD_ID, min_platform=0,
             payload_version=V1, floor=0, body_size=None, alg=ES256, meta=None):
    spec = algorithm_for(alg)
    t = host_trailer.Trailer(
        body_size=len(body) if body_size is None else body_size,
        pad_size=0, meta=meta if meta is not None else {"k": 1},
        board_id=board_id, min_platform_version=min_platform,
        payload_version=payload_version, payload_version_floor=floor,
        key_id=key_id, sig_alg=alg, body_sha256=hashlib.sha256(body).digest())
    t.signature = sign.sign_region(priv, host_trailer.signed_region(t), spec)
    return host_trailer.pack_trailer(t)


def _status(pending, tried, confirmed):
    return host_status.build_status_sector(BLOCK, pending=pending, tried=tried,
                                           confirmed=confirmed)


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


# --- parse_trailer ----------------------------------------------------------

def test_parse_trailer_valid():
    priv, _pub = _key()
    body = b"romfs-body" * 5
    t = B.parse_trailer(_trailer(priv, 0x100, body, payload_version=V1))
    assert t.body_size == len(body) and t.board_id == BOARD_ID
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

def _eval(trailer_bytes, body, status, *, is_front=True, floor=0, board_id=BOARD_ID,
          trusted=None, platform=PLATFORM, verify=_verify):
    return B.evaluate_slot(body, status, trailer_bytes, is_front, floor, board_id,
                           trusted if trusted is not None else {}, platform, verify)


def test_evaluate_front_confirmed_mounts():
    priv, pub = _key()
    body = b"app" * 40
    t, write_tried = _eval(_trailer(priv, 0x100, body), body,
                           _status(True, True, True), trusted={0x100: pub})
    assert write_tried is False and t.body_size == len(body)


def test_evaluate_front_first_trial_arms_tried():
    priv, pub = _key()
    body = b"app" * 40
    _t, write_tried = _eval(_trailer(priv, 0x100, body), body,
                            _status(True, False, False), trusted={0x100: pub})
    assert write_tried is True


@pytest.mark.parametrize(("pending", "tried", "confirmed", "reason"), [
    (True, True, False, "trial-failed"),       # tried but never confirmed
    (False, False, True, "forged-confirm"),    # BACK shape on FRONT
    (False, False, False, "status"),           # nothing set
    (True, False, True, "status"),             # pending+confirmed, no tried
])
def test_evaluate_front_status_rejections(pending, tried, confirmed, reason):
    priv, pub = _key()
    body = b"app" * 40
    with pytest.raises(B.OtaReject, match=reason):
        _eval(_trailer(priv, 0x100, body), body, _status(pending, tried, confirmed),
              trusted={0x100: pub})


def test_evaluate_back_factory_shape_mounts():
    priv, pub = _key()
    body = b"golden" * 20
    t, write_tried = _eval(_trailer(priv, 0x1, body), body, _status(False, False, True),
                           is_front=False, trusted={0x1: pub})
    assert write_tried is False and t.key_id == 0x1


def test_evaluate_back_non_factory_rejected():
    priv, pub = _key()
    body = b"golden" * 20
    with pytest.raises(B.OtaReject, match="back-not-factory"):
        _eval(_trailer(priv, 0x1, body), body, _status(True, True, True),
              is_front=False, trusted={0x1: pub})


def test_evaluate_unknown_or_revoked_key():
    priv, pub = _key()
    body = b"app" * 40
    with pytest.raises(B.OtaReject, match="key"):
        _eval(_trailer(priv, 0x100, body), body, _status(True, True, True),
              trusted={0x999: pub})      # signer's key_id 0x100 not in the trusted set


def test_evaluate_bad_signature():
    priv, pub = _key()
    other, _ = _key()                    # a different keypair: signature won't verify
    body = b"app" * 40
    with pytest.raises(B.OtaReject, match="sig"):
        _eval(_trailer(other, 0x100, body), body, _status(True, True, True),
              trusted={0x100: pub})


def test_evaluate_board_mismatch():
    priv, pub = _key()
    body = b"app" * 40
    with pytest.raises(B.OtaReject, match="board"):
        _eval(_trailer(priv, 0x100, body, board_id=0x9999), body,
              _status(True, True, True), board_id=BOARD_ID, trusted={0x100: pub})


def test_evaluate_board_guard_off_accepts_any():
    priv, pub = _key()
    body = b"app" * 40
    t, _ = _eval(_trailer(priv, 0x100, body, board_id=0xABCD), body,
                 _status(True, True, True), board_id=0, trusted={0x100: pub})
    assert t.board_id == 0xABCD


def test_evaluate_incompatible_platform():
    priv, pub = _key()
    body = b"app" * 40
    with pytest.raises(B.OtaReject, match="compat"):
        _eval(_trailer(priv, 0x100, body, min_platform=(6 << 24)), body,
              _status(True, True, True), platform=(5 << 24), trusted={0x100: pub})


def test_evaluate_body_too_large_for_slot():
    priv, pub = _key()
    body = b"app" * 40
    # trailer claims a body bigger than the provided slot body region
    tb = _trailer(priv, 0x100, body, body_size=len(body) + 1)
    with pytest.raises(B.OtaReject, match="size"):
        _eval(tb, body, _status(True, True, True), trusted={0x100: pub})


def test_evaluate_body_sha_mismatch():
    priv, pub = _key()
    body = b"app" * 40
    tb = _trailer(priv, 0x100, body)
    corrupt = bytearray(body)
    corrupt[0] ^= 0xFF                    # body no longer matches the signed sha
    with pytest.raises(B.OtaReject, match="body-sha"):
        _eval(tb, bytes(corrupt), _status(True, True, True), trusted={0x100: pub})


def test_evaluate_front_rollback():
    priv, pub = _key()
    body = b"app" * 40
    with pytest.raises(B.OtaReject, match="rollback"):
        _eval(_trailer(priv, 0x100, body, payload_version=V1), body,
              _status(True, True, True), floor=(2 << 24), trusted={0x100: pub})


# --- OtaBoot.run (slot selection) -------------------------------------------

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

    def boot(self, trusted, *, board_id=BOARD_ID, platform=PLATFORM):
        return B.OtaBoot(self.read, _verify, self.mount, self.write_marker,
                         PARTITION_SIZE, FRONT_SIZE, BLOCK, board_id, trusted,
                         platform).run()


def _partition(front_slot, back_slot):
    p = bytearray()
    p += front_slot
    p += back_slot
    return p


def _front(priv, key_id, body, status, **kw):
    return _slot(body, _trailer(priv, key_id, body, **kw), status, FRONT_SIZE)


def _back(priv, key_id, body, **kw):
    return _slot(body, _trailer(priv, key_id, body, **kw),
                 _status(False, False, True), PARTITION_SIZE - FRONT_SIZE)


def test_run_mounts_confirmed_front():
    priv, pub = _key()
    fb, bb = b"frontimg" * 4, b"backimg" * 4
    dev = _Dev(_partition(_front(priv, 0x100, fb, _status(True, True, True)),
                          _back(priv, 0x1, bb)))
    slot, t, reason = dev.boot({0x100: pub, 0x1: pub})
    assert slot == "FRONT" and reason is None
    assert dev.mounted == [fb] and dev.writes == []
    assert t.payload_version == V1


def test_run_first_trial_arms_tried_and_mounts_front():
    priv, pub = _key()
    fb, bb = b"trialimg" * 4, b"backimg" * 4
    dev = _Dev(_partition(_front(priv, 0x100, fb, _status(True, False, False)),
                          _back(priv, 0x1, bb)))
    slot, _t, _r = dev.boot({0x100: pub, 0x1: pub})
    assert slot == "FRONT" and dev.mounted == [fb]
    # 'tried' was written into FRONT's status sector at the right absolute offset
    assert dev.writes == [(FRONT_SIZE - 2 * BLOCK + B._TRIED_OFF, B.TRIED)]


def test_run_falls_back_to_back_on_front_failure():
    priv, pub = _key()
    other, _ = _key()
    fb, bb = b"frontimg" * 4, b"backimg" * 4
    # FRONT signed by an untrusted key -> 'sig'; BACK is a valid golden slot
    dev = _Dev(_partition(_front(other, 0x100, fb, _status(True, True, True)),
                          _back(priv, 0x1, bb)))
    slot, t, reason = dev.boot({0x100: pub, 0x1: pub})
    assert slot == "BACK" and reason == "sig"
    assert dev.mounted == [bb] and t.key_id == 0x1


def test_run_front_rollback_floored_by_back():
    priv, pub = _key()
    fb, bb = b"oldfront" * 4, b"newback" * 4
    # BACK is newer (v2); FRONT is v1 -> FRONT rejected 'rollback', BACK mounts.
    dev = _Dev(_partition(
        _front(priv, 0x100, fb, _status(True, True, True), payload_version=V1),
        _back(priv, 0x1, bb, payload_version=(2 << 24))))
    slot, _t, reason = dev.boot({0x100: pub, 0x1: pub})
    assert slot == "BACK" and reason == "rollback"


def test_run_torn_back_floors_front_at_zero():
    priv, pub = _key()
    fb, bb = b"frontimg" * 4, b"backimg" * 4
    front = _front(priv, 0x100, fb, _status(True, True, True), payload_version=0)
    back = _back(priv, 0x1, bb)
    back[PARTITION_SIZE - FRONT_SIZE - BLOCK] ^= 0xFF   # corrupt BACK's trailer magic
    dev = _Dev(_partition(front, back))
    slot, _t, reason = dev.boot({0x100: pub, 0x1: pub})
    assert slot == "FRONT" and reason is None   # floor fell back to 0, so v0 is allowed


def test_run_both_slots_fail_raises():
    priv, pub = _key()
    other, _ = _key()
    fb, bb = b"frontimg" * 4, b"backimg" * 4
    # both signed by an untrusted key
    dev = _Dev(_partition(_front(other, 0x100, fb, _status(True, True, True)),
                          _back(other, 0x1, bb)))
    with pytest.raises(B.OtaReject, match="no-slot:sig/sig"):
        dev.boot({0x100: pub, 0x1: pub})


def test_run_hung_trial_rolls_back_on_next_boot():
    priv, pub = _key()
    fb, bb = b"trialimg" * 4, b"backimg" * 4
    dev = _Dev(_partition(_front(priv, 0x100, fb, _status(True, False, False)),
                          _back(priv, 0x1, bb)))
    trusted = {0x100: pub, 0x1: pub}
    slot1, _t, _r = dev.boot(trusted)            # first boot: arms 'tried', mounts FRONT
    assert slot1 == "FRONT"
    slot2, _t2, reason = dev.boot(trusted)       # app hung (never confirmed) -> reboot
    assert slot2 == "BACK" and reason == "trial-failed"

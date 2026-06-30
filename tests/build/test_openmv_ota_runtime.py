"""Host tests for the device-side ``openmv_ota`` runtime library (the package
scaffolded into a project's ``app/lib/openmv_ota/``).

Like ``test_device_boot`` for ``boot.py``, this exercises the pure logic on the
host and pins the duplicated status-marker constants against the originals in
``openmv_ota.ota.status`` so they can't drift. The device entry points
(``status``/``confirm``/``sync``) wire MicroPython-only I/O and are covered under
QEMU, not here.
"""

from __future__ import annotations

import pytest

from openmv_ota.build.device import openmv_ota as rt
from openmv_ota.ota import status as host_status


def _sector(pending, tried, confirmed):
    return host_status.build_status_sector(4096, pending=pending, tried=tried,
                                           confirmed=confirmed)


def test_markers_and_offsets_pinned_to_host():
    # The library duplicates these from openmv_ota.ota.status; keep them identical.
    assert (rt.PENDING, rt.TRIED, rt.CONFIRMED) == (
        host_status.PENDING, host_status.TRIED, host_status.CONFIRMED)
    assert (rt.REPR_FULL, rt.REPR_DELTA) == (host_status.REPR_FULL, host_status.REPR_DELTA)
    assert (rt._PENDING_OFF, rt._TRIED_OFF, rt._CONFIRMED_OFF, rt._REPR_OFF) == (
        host_status.PENDING_OFFSET, host_status.TRIED_OFFSET, host_status.CONFIRMED_OFFSET,
        host_status.REPR_OFFSET)
    assert rt.MARKER_SIZE == host_status.MARKER_SIZE


def test_rollback_mirror_matches_host():
    from openmv_ota.ota import rollback as host
    assert rt._ROLLBACK_ENTRY == host.ENTRY_SIZE
    assert rt._rollback_entry(0x01020000) == host.encode_entry(0x01020000)
    sector = bytearray(b"\xff" * 4096)
    sector[0:host.ENTRY_SIZE] = host.encode_entry(0x01000000)
    sector[host.ENTRY_SIZE:2 * host.ENTRY_SIZE] = host.encode_entry(0x01030000)
    assert rt._rollback_floor_of(sector) == host.floor_of(sector) == 0x01030000
    assert rt._rollback_append_offset(sector) == host.append_offset(sector) == 2 * host.ENTRY_SIZE
    assert rt._rollback_append_offset(b"\x00" * 4096) is None   # full -> no room


def test_representation_of_decodes_each():
    def sector(repr_marker):
        s = bytearray(_sector(True, True, False))
        if repr_marker is not None:
            s[rt._REPR_OFF:rt._REPR_OFF + rt.MARKER_SIZE] = repr_marker
        return bytes(s)
    assert rt._representation_of(sector(rt.REPR_FULL)) == "full"
    assert rt._representation_of(sector(rt.REPR_DELTA)) == "delta"
    assert rt._representation_of(sector(None)) is None        # unwritten (0xFF) -> None


def test_status_of_confirmed_image():
    s = rt._status_of(_sector(True, True, True))   # post-OTA confirmed / factory shape
    assert s == {"pending": True, "tried": True, "confirmed": True, "trial": False}
    assert rt._needs_confirm(_sector(True, True, True)) is False


def test_status_of_unconfirmed_trial():
    s = rt._status_of(_sector(True, True, False))  # booted a one-shot trial, not yet kept
    assert s["trial"] is True and s["confirmed"] is False
    assert rt._needs_confirm(_sector(True, True, False)) is True


# confirm() acts only when we booted FRONT *and* it's an un-confirmed trial. The slot
# guard is the safety bit -- see the BACK rows.
@pytest.mark.parametrize(("slot", "pending", "tried", "confirmed", "expect"), [
    ("FRONT", True,  True,  False, True),    # booted FRONT, un-confirmed trial -> confirm
    ("BACK",  True,  True,  False, False),   # fell back from a failed trial -> do NOT
    (None,    True,  True,  False, False),   # unknown slot -> never confirm
    ("FRONT", True,  True,  True,  False),   # already confirmed
    ("FRONT", True,  False, False, False),   # pending only, not a trial yet
    ("FRONT", False, False, False, False),   # nothing set
])
def test_should_confirm(slot, pending, tried, confirmed, expect):
    assert rt._should_confirm(slot, _sector(pending, tried, confirmed)) is expect


def test_log_reexport_is_a_null_logger_on_host():
    # openmv_log is absent off-device, so openmv_ota.log is a null logger -- the app can
    # call .info/.warning/etc. unconditionally (on-device it's logging.getLogger).
    assert rt.log.debug("d") is None
    assert rt.log.info("i") is None
    assert rt.log.warning("w") is None
    assert rt.log.error("e") is None
    assert rt.log.critical("c") is None


def test_status_of_pending_only_is_not_a_trial():
    # staged but not yet trial-booted (boot.py hasn't armed 'tried') -> nothing to confirm
    s = rt._status_of(_sector(True, False, False))
    assert s["trial"] is False
    assert rt._needs_confirm(_sector(True, False, False)) is False


def test_status_of_erased_sector():
    s = rt._status_of(_sector(False, False, False))
    assert s == {"pending": False, "tried": False, "confirmed": False, "trial": False}


def test_markers_decodes_each_flag():
    assert rt._markers(_sector(True, False, True)) == (True, False, True)


def _target(buf):
    """A read_target(off, n) over an in-memory buffer (stands in for the partition)."""
    return lambda off, n: buf[off:off + n]


def test_streams_equal_matching():
    # multi-chunk file that matches the target byte-for-byte
    assert rt._streams_equal([b"abcd", b"ef"], _target(b"abcdef")) is True


def test_streams_equal_mismatch():
    assert rt._streams_equal([b"abcd", b"ef"], _target(b"abXdef")) is False


def test_streams_equal_offset_tracking():
    # a later chunk differing is still caught (offset advances per chunk)
    assert rt._streams_equal([b"ab", b"cd", b"ef"], _target(b"abcdXf")) is False


def test_streams_equal_feeds_per_chunk():
    # the watchdog is fed per compared chunk (the already-applied case reads it all)
    calls = []
    assert rt._streams_equal([b"ab", b"cd"], _target(b"abcd"), lambda: calls.append(1)) is True
    assert len(calls) == 2


class _RecordLog:
    def __init__(self):
        self.lines = []

    def info(self, msg, *a):
        self.lines.append(msg)


def test_progress_logs_only_on_each_ten_percent_step(monkeypatch):
    rec = _RecordLog()
    monkeypatch.setattr(rt, "log", rec)
    p = rt._Progress("coprocessor")
    # within the same 10% bucket -> one line; crossing into the next -> another
    for done in (3, 5, 9, 12, 19, 25):
        p(done, 100)
    assert rec.lines == [
        "coprocessor: 3% (3/100 bytes)",
        "coprocessor: 12% (12/100 bytes)",
        "coprocessor: 25% (25/100 bytes)",
    ]


def test_progress_zero_total_is_full(monkeypatch):
    rec = _RecordLog()
    monkeypatch.setattr(rt, "log", rec)
    rt._Progress("coprocessor")(0, 0)          # empty target -> 100%, never divides by zero
    assert rec.lines == ["coprocessor: 100% (0/0 bytes)"]


def test_check_readback_ok():
    rt._check_readback(b"\xff\xff", b"\xff\xff")          # match -> no raise
    rt._check_readback(bytearray(b"abc"), b"abc")         # bytearray vs bytes, by value


def test_check_readback_mismatch_raises():
    import pytest
    with pytest.raises(OSError):
        rt._check_readback(b"\xff\x00", b"\xff\xff")      # erase/write didn't take

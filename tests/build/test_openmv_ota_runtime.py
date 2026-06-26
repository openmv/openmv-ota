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
    assert (rt._PENDING_OFF, rt._TRIED_OFF, rt._CONFIRMED_OFF) == (
        host_status.PENDING_OFFSET, host_status.TRIED_OFFSET, host_status.CONFIRMED_OFFSET)
    assert rt.MARKER_SIZE == host_status.MARKER_SIZE


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


def test_log_reexport_is_a_noop_on_host():
    # _ota_log is absent off-device, so openmv_ota.log degrades to a no-op (the app can
    # still call it unconditionally).
    assert rt.log("app", "hello") is None


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


def test_check_readback_ok():
    rt._check_readback(b"\xff\xff", b"\xff\xff")          # match -> no raise
    rt._check_readback(bytearray(b"abc"), b"abc")         # bytearray vs bytes, by value


def test_check_readback_mismatch_raises():
    import pytest
    with pytest.raises(OSError):
        rt._check_readback(b"\xff\x00", b"\xff\xff")      # erase/write didn't take

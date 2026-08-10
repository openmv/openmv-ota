"""Tests for the slot status-sector markers."""

from __future__ import annotations

from openmv_ota.ota import status

FF16 = b"\xff" * 16


def test_markers_distinct_16_bytes():
    ms = [status.PENDING, status.TRIED, status.CONFIRMED]
    assert all(len(m) == 16 for m in ms)
    assert len(set(ms)) == 3                       # all distinct
    assert all(m not in (FF16, b"\x00" * 16) for m in ms)  # not erased / all-zero


def test_front_status_sector():
    s = status.build_status_sector(4096, pending=True, tried=True, confirmed=True)
    assert len(s) == 4096
    assert s[0:16] == status.PENDING
    assert s[16:32] == status.TRIED
    assert s[32:48] == status.CONFIRMED
    assert s[48:] == b"\xff" * (4096 - 48)


def test_back_status_sector():
    s = status.build_status_sector(4096, pending=False, tried=False, confirmed=True)
    assert s[0:16] == FF16 and s[16:32] == FF16      # not staged, not tried
    assert s[32:48] == status.CONFIRMED              # golden / factory state


def test_repr_markers_distinct():
    ms = [status.PENDING, status.TRIED, status.CONFIRMED, status.REPR_FULL, status.REPR_DELTA]
    assert all(len(m) == 16 for m in ms) and len(set(ms)) == 5   # all distinct


def test_encode_counter_is_value_plus_complement():
    assert status.encode_counter(1) == b"\x01\x00\x00\x00\xfe\xff\xff\xff"
    assert len(status.encode_counter(0xFFFFFFFF)) == status.COUNTER_SIZE
    # a blank field must never decode as a real counter: 0xFF.. is not its own complement
    assert status.encode_counter(0xFFFFFFFF) != b"\xff" * status.COUNTER_SIZE


def test_status_sector_counter_is_optional_and_placed():
    off, n = status.COUNTER_OFFSET, status.COUNTER_SIZE
    s = status.build_status_sector(4096, pending=False, tried=False, confirmed=True, counter=3)
    assert s[off:off + n] == status.encode_counter(3)
    assert s[off + n:] == b"\xff" * (4096 - off - n)          # attempts region left blank
    bare = status.build_status_sector(4096, pending=False, tried=False, confirmed=True)
    assert bare[off:off + n] == b"\xff" * n                    # no counter unless asked


def test_build_status_sector_leaves_repr_unwritten():
    # repr is install-time provenance only -- a factory status sector never sets it
    s = status.build_status_sector(4096, pending=True, tried=True, confirmed=True)
    assert s[status.REPR_OFFSET:status.REPR_OFFSET + 16] == FF16

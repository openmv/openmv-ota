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


def test_build_status_sector_leaves_repr_unwritten():
    # repr is install-time provenance only -- a factory status sector never sets it
    s = status.build_status_sector(4096, pending=True, tried=True, confirmed=True)
    assert s[status.REPR_OFFSET:status.REPR_OFFSET + 16] == FF16

"""Tests for the anti-rollback floor log (openmv_ota.ota.rollback)."""

from __future__ import annotations

from openmv_ota.ota import rollback


def _sector(*versions, size=4096):
    s = bytearray(b"\xff" * size)
    off = 0
    for v in versions:
        s[off:off + rollback.ENTRY_SIZE] = rollback.encode_entry(v)
        off += rollback.ENTRY_SIZE
    return bytes(s)


def test_blank_sector_has_zero_floor():
    assert rollback.floor_of(b"\xff" * 4096) == 0
    assert rollback.append_offset(b"\xff" * 4096) == 0


def test_floor_is_the_max_entry():
    s = _sector(0x01000000, 0x01010000, 0x01000500)   # 1.0.0, 1.1.0, 1.0.5
    assert rollback.floor_of(s) == 0x01010000          # the highest
    assert rollback.append_offset(s) == 3 * rollback.ENTRY_SIZE   # after the 3 entries


def test_torn_entry_is_ignored():
    s = bytearray(_sector(0x01000000))
    # a torn second entry: version written, complement still blank -> invalid
    s[rollback.ENTRY_SIZE:rollback.ENTRY_SIZE + 4] = b"\x00\x00\x02\x01"
    s = bytes(s)
    assert rollback.floor_of(s) == 0x01000000          # torn entry doesn't raise the floor
    # ...and append skips past the torn slot to the next blank one
    assert rollback.append_offset(s) == 2 * rollback.ENTRY_SIZE


def test_full_sector_has_no_append_room():
    full = _sector(*range(1, 4096 // rollback.ENTRY_SIZE + 1), size=4096)
    assert rollback.append_offset(full) is None        # caller must erase + compact
    assert rollback.floor_of(full) == 4096 // rollback.ENTRY_SIZE


def test_entry_roundtrip():
    e = rollback.encode_entry(0x12345678)
    assert len(e) == rollback.ENTRY_SIZE
    assert rollback.floor_of(e) == 0x12345678

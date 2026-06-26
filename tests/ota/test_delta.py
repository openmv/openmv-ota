"""Tests for the OTA copy/insert delta codec (openmv_ota.ota.delta)."""

from __future__ import annotations

import pytest

from openmv_ota.ota.delta import (
    MAGIC,
    _read_svarint,
    _read_uvarint,
    _write_svarint,
    _write_uvarint,
    apply_delta,
    make_delta,
)
from openmv_ota.ota.errors import OtaError


def _roundtrip(base, target):
    patch = make_delta(base, target)
    assert apply_delta(base, patch) == target
    return patch


# --- varints ----------------------------------------------------------------

@pytest.mark.parametrize("val", [0, 1, 127, 128, 255, 300, 16384, 1 << 20, (1 << 32) - 1])
def test_uvarint_roundtrip(val):
    out = bytearray()
    _write_uvarint(out, val)
    got, pos = _read_uvarint(bytes(out), 0)
    assert got == val and pos == len(out)


@pytest.mark.parametrize("val", [0, 1, -1, 127, -128, 4096, -4096, (1 << 24), -(1 << 24)])
def test_svarint_roundtrip(val):
    out = bytearray()
    _write_svarint(out, val)
    got, pos = _read_svarint(bytes(out), 0)
    assert got == val and pos == len(out)


# --- make/apply roundtrips --------------------------------------------------

def test_identical_is_one_big_copy():
    base = bytes(range(256)) * 200                      # 51200 bytes
    patch = _roundtrip(base, base)
    assert len(patch) < len(base) // 10                 # tiny: essentially one copy op


def test_small_edit_in_large_image():
    base = bytearray(bytes(range(256)) * 200)
    target = bytearray(base)
    target[10000:10016] = b"NEW-CERT-BYTES!!"           # a small in-place edit
    patch = _roundtrip(bytes(base), bytes(target))
    assert len(patch) < 1000                            # the unchanged bulk stays a copy


def test_inserted_region_shifts_everything():
    base = bytes(range(256)) * 100
    target = base[:5000] + b"X" * 300 + base[5000:]     # an insertion slides the tail
    patch = _roundtrip(base, target)
    assert len(patch) < len(target) // 5                # the slid tail is still a copy (seek)


def test_deleted_region():
    base = bytes(range(256)) * 100
    target = base[:5000] + base[5400:]                  # a deletion
    _roundtrip(base, target)


def test_block_moved_backward():
    # a chunk that moved earlier in the address space -> a negative seek
    block = bytes(range(200)) * 10
    base = b"AAAA" * 500 + block + b"BBBB" * 500
    target = block + b"AAAA" * 500 + b"BBBB" * 500
    _roundtrip(base, target)


def test_all_literal_when_nothing_matches():
    base = b"\x00" * 4096
    target = bytes((i * 7 + 3) & 0xFF for i in range(5000))   # no anchor-length run in base
    patch = _roundtrip(base, target)
    assert b"OCDL" == patch[:4] and len(patch) >= len(target)  # ~all inserted


def test_empty_target():
    assert apply_delta(b"abc", make_delta(b"abc", b"")) == b""


def test_target_shorter_than_anchor():
    _roundtrip(b"the base image is long enough to index" * 4, b"tiny")


def test_base_shorter_than_anchor():
    _roundtrip(b"hi", b"hello world this is the new target content")


# --- rejections -------------------------------------------------------------

def test_apply_bad_magic():
    with pytest.raises(OtaError, match="not an OCDL"):
        apply_delta(b"base", b"XXXX\x00")


def test_apply_too_short():
    with pytest.raises(OtaError, match="not an OCDL"):
        apply_delta(b"base", b"OC")


def test_apply_copy_out_of_bounds():
    # a hand-built patch whose copy runs past the base -> rejected, not silent
    out = bytearray(MAGIC)
    _write_uvarint(out, 100)            # target_size
    _write_uvarint(out, 0)              # insert_len
    _write_uvarint(out, 100)            # copy_len (base is only 4 bytes)
    _write_svarint(out, 0)             # seek
    with pytest.raises(OtaError, match="out of base bounds"):
        apply_delta(b"base", bytes(out))


def test_apply_size_mismatch():
    out = bytearray(MAGIC)
    _write_uvarint(out, 999)           # claims 999 bytes...
    _write_uvarint(out, 3)             # ...but only inserts 3
    _write_uvarint(out, 0)
    _write_svarint(out, 0)
    out += b"abc"
    with pytest.raises(OtaError, match="header says"):
        apply_delta(b"base", bytes(out))

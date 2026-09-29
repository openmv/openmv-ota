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


def _gz(data):
    import gzip
    return gzip.compress(data, mtime=0)


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

def test_identical_compresses_away():
    base = bytes(range(256)) * 200                      # 51200 bytes
    patch = _roundtrip(base, base)
    # the raw patch is image-sized (a diff of zeros) but gzips to almost nothing
    assert len(_gz(patch)) < len(base) // 50


def test_small_edit_gzips_tiny():
    base = bytearray(bytes(range(256)) * 200)
    target = bytearray(base)
    target[10000:10016] = b"NEW-CERT-BYTES!!"           # a small in-place edit
    patch = _roundtrip(bytes(base), bytes(target))
    assert len(_gz(patch)) < 1000                       # the unchanged bulk -> zero diff


def test_scattered_edits_fold_into_one_diff():
    # bsdiff's win: every 64th byte changed (e.g. shifted pointers) is ONE diff region
    # (sparse nonzeros) that gzips small -- copy/insert would re-insert all of it.
    base = bytes((i * 5) & 0xFF for i in range(20000))
    target = bytearray(base)
    for i in range(0, len(target), 64):
        target[i] ^= 0xAA
    patch = _roundtrip(base, bytes(target))
    assert len(_gz(patch)) < len(target) // 3           # far smaller than the full image


def test_inserted_region_shifts_everything():
    base = bytes(range(256)) * 100
    target = base[:5000] + b"X" * 300 + base[5000:]     # an insertion slides the tail
    patch = _roundtrip(base, target)
    assert len(_gz(patch)) < len(target) // 5           # the slid tail is still a copy (seek)


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
    assert b"OCDL" == patch[:4]


def test_empty_target():
    assert apply_delta(b"abc", make_delta(b"abc", b"")) == b""


def test_target_shorter_than_anchor():
    _roundtrip(b"the base image is long enough to index" * 4, b"tiny")


def test_base_shorter_than_anchor():
    _roundtrip(b"hi", b"hello world this is the new target content")


# --- rejections -------------------------------------------------------------

def test_target_size_reads_header():
    from openmv_ota.ota.delta import target_size
    base = bytes(range(256)) * 50
    target = base[:1000] + b"edit" + base[1000:]
    assert target_size(make_delta(base, target)) == len(target)


def test_target_size_bad_magic():
    from openmv_ota.ota.delta import target_size
    with pytest.raises(OtaError, match="not an OCDL"):
        target_size(b"XX")


def test_summarize_reports_stats():
    from openmv_ota.ota.delta import summarize
    base = bytes((i * 5) & 0xFF for i in range(8000))
    target = bytearray(base)
    target[100:103] = b"abc"                            # one small change
    s = summarize(make_delta(base, bytes(target)))
    assert s["target_size"] == len(target) and s["ops"] >= 1
    assert s["nonzero_diff_bytes"] >= 1 and s["nonzero_diff_bytes"] <= s["diff_bytes"]


def test_summarize_bad_magic():
    from openmv_ota.ota.delta import summarize
    with pytest.raises(OtaError, match="not an OCDL"):
        summarize(b"XX")


def test_summarize_truncated():
    from openmv_ota.ota.delta import summarize
    out = bytearray(MAGIC)
    _write_uvarint(out, 100)            # claims 100 bytes but carries no ops
    with pytest.raises(OtaError, match="truncated"):
        summarize(bytes(out))


def test_apply_bad_magic():
    with pytest.raises(OtaError, match="not an OCDL"):
        apply_delta(b"base", b"XXXX\x00")


def test_apply_too_short():
    with pytest.raises(OtaError, match="not an OCDL"):
        apply_delta(b"base", b"OC")


def test_apply_copy_out_of_bounds():
    # a hand-built patch whose diff region runs past the base -> rejected, not silent
    out = bytearray(MAGIC)
    _write_uvarint(out, 100)            # target_size
    _write_uvarint(out, 0)              # extra_len
    _write_uvarint(out, 100)            # diff_len (base is only 4 bytes)
    _write_svarint(out, 0)             # seek
    out += bytes(100)                  # the diff bytes are there -- the BASE is what is short
    with pytest.raises(OtaError, match="out of base bounds"):
        apply_delta(b"base", bytes(out))


def test_apply_truncated():
    out = bytearray(MAGIC)
    _write_uvarint(out, 100)           # claims 100 bytes but the ops run out
    with pytest.raises(OtaError, match="truncated"):
        apply_delta(b"base", bytes(out))


def test_apply_size_overshoot():
    out = bytearray(MAGIC)
    _write_uvarint(out, 3)             # claims 3 bytes...
    _write_uvarint(out, 5)             # ...but one op emits 5 extra
    _write_uvarint(out, 0)
    _write_svarint(out, 0)
    out += b"abcde"
    with pytest.raises(OtaError, match="header says"):
        apply_delta(b"base", bytes(out))


# --- malformed patches found by the fuzz suite (tests/fuzz/test_fuzz_delta.py) -------------

def test_a_runaway_varint_is_refused_not_decoded():
    """A run of 0xFF is one endless varint: uncapped, it grows a bigint 7 bits per byte --
    quadratic in the patch size, and the server decodes this header off every uploaded delta.
    100 KB of it took seconds; the gzip of a few MB of it is a worker pinned for hours."""
    from openmv_ota.ota.delta import summarize, target_size
    patch = MAGIC + b"\xff" * 100_000 + b"\x01"
    for f in (target_size, summarize, lambda p: apply_delta(b"", p)):
        with pytest.raises(OtaError, match="varint longer than 10 bytes"):
            f(patch)
    # ...while the longest LEGAL varint (10 bytes) still decodes
    out = bytearray()
    _write_uvarint(out, (1 << 64) - 1)
    assert len(out) == 10 and _read_uvarint(bytes(out), 0) == ((1 << 64) - 1, 10)


def test_a_short_diff_run_is_truncation_not_an_index_error():
    """The final op promises 10 diff bytes and the patch carries 2: rejected as truncated.
    It used to slice a short diff and IndexError in the add."""
    patch = MAGIC + bytes([10, 0, 10, 0, 1, 2])   # target|extra|diff|seek, then 2 of 10 bytes
    with pytest.raises(OtaError, match="truncated"):
        apply_delta(bytes(20), patch)


def test_a_short_extra_run_is_truncation_not_a_short_image():
    patch = MAGIC + bytes([10, 10, 0, 0]) + b"abc"   # 10 literal bytes promised, 3 present
    with pytest.raises(OtaError, match="truncated"):
        apply_delta(b"", patch)


def test_an_op_that_emits_nothing_is_refused():
    """(extra 0, diff 0) consumes three patch bytes and produces nothing. The host loop is
    bounded by the patch, but the device's is bounded only by how much a gzip of zeros
    inflates to -- so both refuse it (the device mirror is pinned in test_installer)."""
    patch = MAGIC + bytes([4, 0, 0, 0]) + bytes([0, 4, 0]) + bytes(4)
    with pytest.raises(OtaError, match="no progress"):
        apply_delta(b"base", patch)

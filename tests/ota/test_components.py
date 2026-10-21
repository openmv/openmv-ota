"""Tests for the firmware-component codec (openmv_ota.ota.components)."""

from __future__ import annotations

import hashlib
import lzma

import pytest

from openmv_ota.ota import components as C
from openmv_ota.ota.errors import OtaError
from openmv_ota.ota.trailer import Component

_FW = (bytes(range(256)) * 300) + b"\x00" * 5000 + b"OpenMV" * 700     # compressible, 87 KB


def test_compress_is_raw_lzma1_with_the_fixed_properties():
    blob = C.compress(_FW)
    assert len(blob) < len(_FW) // 4
    # the boot stub's decoder is built for exactly these: a raw stream (no header), lc3 lp0 pb2,
    # a 256 KiB dictionary
    assert C.LZMA_FILTERS[0]["id"] == lzma.FILTER_LZMA1 and C.LZMA_DICT_SIZE == 256 * 1024
    assert (C.LZMA_FILTERS[0]["lc"], C.LZMA_FILTERS[0]["lp"], C.LZMA_FILTERS[0]["pb"]) == (3, 0, 2)
    assert lzma.decompress(blob, format=lzma.FORMAT_RAW, filters=C.LZMA_FILTERS) == _FW


def test_compress_is_deterministic():
    """The same firmware in two releases must be the same bytes, or the delta between them
    carries the whole blob instead of one copy."""
    assert C.compress(_FW) == C.compress(_FW)


def test_decompress_round_trips_and_checks_the_size():
    blob = C.compress(_FW)
    assert C.decompress(blob, len(_FW)) == _FW
    with pytest.raises(OtaError, match="decompresses to %d bytes, trailer says %d" % (len(_FW), 7)):
        C.decompress(blob, 7)
    with pytest.raises(OtaError, match="does not decompress"):
        C.decompress(b"\xff" * 64, len(_FW))


def test_append_aligns_each_blob_and_describes_it():
    body = b"R" * 1001                       # not a multiple of 16
    b1, b2 = C.compress(_FW), C.compress(_FW[:100])
    image, comps = C.append(body, [(0x70090000, len(_FW), b1), (0x70300000, 100, b2)])
    assert image[:1001] == body and image[1001:1008] == b"\xff" * 7
    assert comps[0] == Component(0x70090000, 1008, len(b1), len(_FW), hashlib.sha256(b1).digest())
    assert comps[1].offset % 16 == 0 and comps[1].offset >= 1008 + len(b1)
    assert image[comps[1].offset:comps[1].offset + len(b2)] == b2
    assert comps[1].dest == 0x70300000 and comps[1].installed_size == 100
    assert len(image) == comps[1].offset + len(b2)        # nothing after the last blob


def test_append_with_no_blobs_is_the_body():
    assert C.append(b"abc", []) == (b"abc", [])


def test_check_accepts_the_image_it_came_from_and_names_what_is_wrong():
    image, comps = C.append(b"R" * 64, [(1, len(_FW), C.compress(_FW))])
    assert C.check(image, comps) is None
    assert C.check(image, []) is None
    assert "runs past the" in C.check(image[:-1], comps)
    tampered = bytearray(image)
    tampered[comps[0].offset + 3] ^= 0x01
    assert C.check(bytes(tampered), comps) == "component at +%d hash mismatch" % comps[0].offset

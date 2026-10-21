"""Tests for the boot stub's install record codec (openmv_ota.ota.install_record)."""

from __future__ import annotations

import struct

import pytest

from openmv_ota.ota import install_record as R
from openmv_ota.ota.errors import OtaError


def _rec(**over):
    f = dict(stub_version=3, slot=1, reason=R.REASON_TRIAL, hashes=[b"\x11" * 32, b"\x22" * 32])
    f.update(over)
    return R.InstallRecord(**f)


def test_round_trip_and_layout():
    out = R.pack(_rec())
    assert len(out) == 32 + 2 * 32 + 4 and R.FIXED_SIZE == 32
    assert out[:4] == b"OMVI"
    assert struct.unpack_from("<IIIII", out, 4) == (1, 3, 1, 2, R.REASON_TRIAL)
    assert out[32:64] == b"\x11" * 32
    r = R.parse(out)
    assert r == _rec() and r.slot_name == "B"
    assert R.parse(out + b"\xff" * 100) == r          # the rest of the erased sector


def test_a_record_with_no_components_round_trips():
    r = _rec(hashes=[], reason=R.REASON_NORMAL, slot=0)
    assert R.parse(R.pack(r)) == r and r.slot_name == "A"


@pytest.mark.parametrize(("over", "msg"), [
    ({"slot": 2}, "slot must be 0 or 1"),
    ({"reason": 9}, "unknown install record reason"),
    ({"hashes": [b"\x00" * 32] * 17}, "over the 16 limit"),
    ({"hashes": [b"\x00" * 31]}, "hash must be 32 bytes"),
])
def test_pack_rejects(over, msg):
    with pytest.raises(OtaError, match=msg):
        R.pack(_rec(**over))


def _raw(**over):
    f = dict(magic=b"OMVI", version=1, stub_version=1, slot=0, comp_count=0, reason=0)
    f.update(over)
    body = struct.pack(R.FIXED_STRUCT, f["magic"], f["version"], f["stub_version"], f["slot"],
                       f["comp_count"], f["reason"], 0)
    import binascii
    return body + struct.pack("<I", binascii.crc32(body) & 0xFFFFFFFF)


@pytest.mark.parametrize(("data", "msg"), [
    (b"\xff" * 10, "too small"),
    (_raw(magic=b"OMVX"), "bad install record magic"),
    (_raw(version=2), "unsupported install record version"),
    (_raw(comp_count=17), "over the 16 limit"),
    (_raw(comp_count=1), "truncated"),
    (_raw()[:-1] + b"\x00", "crc32 mismatch"),
    (_raw(slot=5), "names slot 5"),
    (_raw(reason=7), "unknown install record reason"),
])
def test_parse_rejects(data, msg):
    with pytest.raises(OtaError, match=msg):
        R.parse(data)


def test_reason_names():
    assert R.REASONS == {0: "normal", 1: "trial", 2: "reverted", 3: "reinstalled"}

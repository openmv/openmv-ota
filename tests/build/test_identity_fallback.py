"""The Alif board-id fallback (device/openmv_ota/__init__.py::_board_id_fallback).

omv.board_id() returns "" on the Alif, because py_omv.c guards on `#ifdef
OMV_BOARD_UID_ADDR` and that name is an extern array there rather than a macro. The
fallback rebuilds the same string from machine.unique_id(). This pins the arithmetic
against a SIMULATION of the C it has to agree with, so a wrong rule cannot pass by
looking plausible -- and against a real AE3's id read off the bench.
"""

import importlib.util
import struct
import sys
import types
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src/openmv_ota/build/device/openmv_ota/__init__.py"


def _load():
    spec = importlib.util.spec_from_file_location("openmv_ota._identity_under_test", str(_SRC))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _c_board_id(raw8: bytes) -> str:
    """What omv.board_id() prints on the Alif once py_omv.c's guard is fixed.

    se_services_get_unique_id() fills 8 bytes into uint8_t[12] (tail zero); alif_hal.c
    reverses all 12 in place; py_omv.c prints the LE words at offsets 8, 4, 0 as %08X.
    """
    arr = bytearray(12)
    arr[0:8] = raw8
    arr.reverse()
    def word(off):
        return struct.unpack_from("<I", arr, off)[0]
    return "%08X%08X%08X" % (word(8), word(4), word(0))


def _fake_machine(uid):
    m = types.ModuleType("machine")
    m.unique_id = lambda: uid
    return m


def test_the_fallback_reproduces_what_the_firmware_would_print(monkeypatch):
    mod = _load()
    real = bytes.fromhex("08636d4000000000")          # the AE3 on the bench
    monkeypatch.setattr(sys, "platform", "alif")
    monkeypatch.setitem(sys.modules, "machine", _fake_machine(real))
    got = mod._board_id_fallback()
    assert got == _c_board_id(real), "the fallback must agree with the C it stands in for"
    assert got == "08636D400000000000000000"
    assert len(got) == 24 and got.isupper()


def test_the_fallback_is_for_one_port_only(monkeypatch):
    # rp2 reaches the same empty board_id, but its words are swapped and not padded, so
    # this arithmetic would be WRONG there. Refuse rather than invent an id.
    mod = _load()
    monkeypatch.setattr(sys, "platform", "rp2")
    monkeypatch.setitem(sys.modules, "machine", _fake_machine(b"\x01" * 8))
    assert mod._board_id_fallback() == ""


def test_the_fallback_refuses_an_id_it_does_not_recognise(monkeypatch):
    mod = _load()
    monkeypatch.setattr(sys, "platform", "alif")
    monkeypatch.setitem(sys.modules, "machine", _fake_machine(b""))
    assert mod._board_id_fallback() == ""
    monkeypatch.setitem(sys.modules, "machine", _fake_machine(b"\x02" * 16))
    assert mod._board_id_fallback() == ""

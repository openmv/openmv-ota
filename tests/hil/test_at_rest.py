"""Encryption at rest, checked from outside the chip: after the happy-path update on a board
whose stub encrypts, the harness reads the FIRMWARE partition back through the bootloader (which
runs before the stub, with the cipher engine off) and fails the scenario unless the flash chip
holds ciphertext. at_rest.judge is the judgement; ota_cycle.check_at_rest the read."""

import os
import random
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "ci", "hil"))
os.environ.setdefault("WIFI_SSID", "")
os.environ.setdefault("WIFI_PASSWORD", "")

import at_rest  # noqa: E402
import ota_cycle  # noqa: E402
from openmv_ota.romfs.boards import firmware_image_name, get_board  # noqa: E402

BOARD = "OPENMV_N6"
LAYOUT = get_board(BOARD).stub
START = LAYOUT.firmware_dest - LAYOUT.origin
END = START + LAYOUT.firmware_max


def _firmware(n=40000):
    """Something shaped like a firmware image: code-like bytes (a narrow alphabet, as machine
    code has), strings, a run of padding."""
    body = bytes(random.Random(1).choices(range(96), k=n - 4096))
    return body + b"MicroPython v1.29 OPENMV_N6 with STM32N657\x00" * 20 + b"\xff" * (4096 - 860)


def _partition(stub, stored, tail=b""):
    """A FIRMWARE partition read-back: the stub's sector, the firmware area holding ``stored``
    (then ``tail``, then blank up to the record), the record's sector, dfu-util's 4 junk bytes."""
    area = stored + tail
    return (stub + b"\xff" * (START - len(stub)) + area + b"\xff" * (END - START - len(area))
            + b"OMVI" + b"\xff" * 4092 + b"\x00" * 4)


STUB = b"\x00\x20\x00\x34" + bytes(range(256)) * 20
FW = _firmware()
CIPHER = os.urandom(len(FW) + (-len(FW)) % 16)           # what an encrypting stub leaves there


def _judge(dump, fw=FW):
    return at_rest.judge(dump, STUB, fw, START, END)


def test_ciphertext_with_a_blank_remainder_passes():
    problems, n = _judge(_partition(STUB, CIPHER))
    assert problems == []
    assert n["stub_ok"] and n["same"] == 0 and n["found"] == 0 and n["stale"] == 0
    assert n["blocks"] == len(FW) // 16 and n["entropy"] > at_rest.MIN_ENTROPY


def test_a_firmware_stored_as_built_fails_every_way():
    problems, n = _judge(_partition(STUB, FW))
    assert n["same"] == n["blocks"] and n["found"] > 0 and n["entropy"] < at_rest.MIN_ENTROPY
    why = " ".join(problems)
    assert "stored exactly as built" in why and "samples of the build" in why and "entropy" in why


def test_some_other_firmware_in_the_clear_fails_on_entropy_alone():
    other = bytes(random.Random(2).choices(range(96), k=len(FW)))   # nothing of this build
    problems, n = _judge(_partition(STUB, other))
    assert n["same"] == 0 and n["found"] == 0
    assert len(problems) == 1 and "entropy" in problems[0]


def test_plaintext_left_after_the_firmware_fails():
    # an older, longer firmware's tail in the clear past the end of an encrypted one
    problems, n = _judge(_partition(STUB, CIPHER, tail=b"\xff" * 4096 + FW[2000:6000]))
    assert n["same"] == 0 and n["stale"] > 3000 and n["found"] > 0
    assert any("not blank" in p for p in problems)


def test_the_wrong_stub_and_a_short_read_are_reported():
    problems, n = _judge(_partition(b"\x11" * len(STUB), CIPHER))
    assert not n["stub_ok"] and "stub" in problems[0]
    problems, n = _judge(_partition(STUB, CIPHER)[:END - 1])
    assert n == {} and "does not cover the firmware area" in problems[0]
    assert at_rest.samples(b"\xff" * 4096) == []          # blank flash would "contain" padding
    assert at_rest.entropy(b"") == 0.0 and at_rest.entropy(b"\x00" * 64) == 0.0


# -- the read ------------------------------------------------------------------------------

@pytest.fixture
def bench(tmp_path, monkeypatch):
    build = tmp_path / "build"
    build.mkdir()
    (build / ("%s-stub.bin" % BOARD)).write_bytes(STUB)
    (build / firmware_image_name(BOARD)).write_bytes(FW)
    monkeypatch.setitem(ota_cycle.CFG, "project", str(tmp_path))
    monkeypatch.setitem(ota_cycle.CFG, "dfu", "DFU")
    holds = {"dump": _partition(STUB, CIPHER), "ran": [], "exec": [], "rc": 0, "logged": []}

    def sh(argv, **k):
        holds["ran"].append(argv)
        if holds["rc"] == 0:
            with open(argv[-1], "wb") as f:              # dfu-util -U <file>
                f.write(holds["dump"])
        return holds["rc"], "dfu-util output"

    monkeypatch.setattr(ota_cycle, "sh", sh)
    monkeypatch.setattr(ota_cycle, "log", holds["logged"].append)
    monkeypatch.setattr(ota_cycle, "device_exec",
                        lambda code, **k: holds["exec"].append(code) or (0, ""))
    monkeypatch.setattr(ota_cycle, "dfu_reset_catch",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("not needed")))
    return holds


def test_the_read_enters_dfu_uploads_the_firmware_alt_and_leaves(bench):
    ok, report = ota_cycle.check_at_rest(BOARD)
    assert ok and report["problems"] == [] and report["same"] == 0 and report["stub_ok"]
    assert bench["exec"] == ["import machine; machine.bootloader()"]
    (argv,) = bench["ran"]
    assert argv[:7] == ["DFU", "-w", "-d", ",37c5:9206", "-a", "1", "--reset"] and argv[-2] == "-U"
    assert not os.path.exists(os.path.dirname(argv[-1]))                # the dump is not kept
    assert any("0 of %d firmware blocks stored as built" % (len(FW) // 16) in ln
               for ln in bench["logged"])


def test_plaintext_in_the_chip_is_a_failure_with_its_reasons(bench):
    bench["dump"] = _partition(STUB, FW)
    ok, report = ota_cycle.check_at_rest(BOARD)
    assert not ok and report["same"] == report["blocks"]
    assert any("NOT ENCRYPTED -- " in ln and "stored exactly as built" in ln for ln in bench["logged"])


def test_with_the_cdc_gone_it_catches_the_dfu_window_and_no_read_is_a_failure(bench, monkeypatch):
    bench["rc"] = 74                                     # dfu-util found nothing to read
    caught = []

    def catch(board, argv, **k):
        caught.append(argv)
        with open(argv[-1], "wb") as f:
            f.write(bench["dump"])
        return 0, ""

    monkeypatch.setattr(ota_cycle, "dfu_reset_catch", catch)
    ok, _report = ota_cycle.check_at_rest(BOARD)
    assert ok and caught and caught[0][-2] == "-U"
    monkeypatch.setattr(ota_cycle, "dfu_reset_catch", lambda *a, **k: (1, "no DFU device"))
    ok, report = ota_cycle.check_at_rest(BOARD)
    assert not ok and "the read-back did not happen" in report["problems"][0]


def test_the_ae3_runs_rollback_for_its_stubs_revert_path():
    ae3 = ota_cycle.regression_scenarios("OPENMV_AE3", ota_cycle.BOARDS["OPENMV_AE3"]["network"])
    assert ae3[:2] == ["delta", "rollback"] and "watchdog" in ae3
    assert {"stub.slot_rejected", "stub.boot_reverted"} <= \
        ota_cycle.scenario_markers("OPENMV_AE3", "rollback")[0]

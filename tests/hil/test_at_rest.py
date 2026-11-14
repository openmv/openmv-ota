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


# -- the slots -----------------------------------------------------------------------------

SLOT = 16 * 4096                       # a small slot: 14 body blocks, status sector, trailer


def _slot_image(body, tag=b""):
    """A plaintext slot image: body, 0xFF pad, a status sector, a trailer naming the body."""
    from tests import trust
    trailer = trust.trailer(trust.ROOT_PRIV, body, meta={"t": tag.decode()})
    control = trailer.ljust(4096, b"\xff")
    return body.ljust(SLOT - 2 * 4096, b"\xff") + bytes(4096) + control


def _ciphered(image, csize):
    """The image as an encrypting stub stores it: random bytes over the ciphered range, the
    rest as it is (a pad left blank, the control sectors in the clear)."""
    return os.urandom(csize) + image[csize:]


def _csize(image):
    from openmv_ota.ota import slot_crypt
    from openmv_ota.ota.trailer import parse_trailer
    return slot_crypt.crypt_size(parse_trailer(image[SLOT - 4096:]), slot_body_end=SLOT - 2 * 4096)


def test_two_ciphered_slots_of_known_images_pass():
    a, b = _slot_image(os.urandom(5000), b"a"), _slot_image(os.urandom(9000), b"b")
    dump = _ciphered(a, _csize(a)) + _ciphered(b, _csize(b))
    problems, numbers = at_rest.judge_slots(dump, SLOT, [a, b])
    assert problems == []
    assert [n["slot"] for n in numbers["slots"]] == ["A", "B"]
    assert all(n["same"] == 0 and n["found"] == 0 and n["stale"] == 0 for n in numbers["slots"])
    assert numbers["slots"][0]["blocks"] == 8192 // 16            # 5000 -> 8 KiB ciphered


def test_a_slot_stored_as_built_fails_every_way():
    a, b = _slot_image(os.urandom(5000), b"a"), _slot_image(os.urandom(9000), b"b")
    dump = a + _ciphered(b, _csize(b))                            # A in the clear
    problems, numbers = at_rest.judge_slots(dump, SLOT, [a, b])
    assert numbers["slots"][0]["same"] == numbers["slots"][0]["blocks"]
    assert any("slot A: " in p and "stored exactly as built" in p for p in problems)
    assert any("slot A: " in p and "samples of its image" in p for p in problems)
    assert any("slot A: " in p and "entropy" in p for p in problems)
    assert not any("slot B" in p for p in problems)


def test_an_unknown_trailer_a_dirty_pad_and_a_short_read_are_reported():
    a, b = _slot_image(os.urandom(5000), b"a"), _slot_image(os.urandom(9000), b"b")
    other = _slot_image(os.urandom(100), b"x")
    dump = _ciphered(other, _csize(other)) + _ciphered(b, _csize(b))
    problems, numbers = at_rest.judge_slots(dump, SLOT, [a, b])
    assert problems == ["slot A holds no trailer of a known image"] and numbers["slots"][0] == {"slot": "A"}
    ca = _csize(a)
    dirty = bytearray(_ciphered(a, ca))
    dirty[ca + 100:ca + 116] = b"\x00" * 16                      # something left in the pad
    problems, numbers = at_rest.judge_slots(bytes(dirty) + _ciphered(b, _csize(b)), SLOT, [a, b])
    assert problems == ["slot A: 16 bytes between its body and its status sector are not blank"]
    problems, numbers = at_rest.judge_slots(b"\xff" * SLOT, SLOT, [a, b])
    assert "does not cover two" in problems[0] and numbers == {}


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
    assert any("no slot images in build/" in ln for ln in bench["logged"])   # nothing published here


def test_with_the_slot_images_the_romfs_partition_is_read_and_judged_too(bench, tmp_path, monkeypatch):
    a, b = _slot_image(os.urandom(5000), b"a"), _slot_image(os.urandom(9000), b"b")
    build = tmp_path / "build"
    import gzip
    (build / ("%s-ota.img.gz" % BOARD)).write_bytes(gzip.compress(a))
    (build / ("%s-factory-romfs.img" % BOARD)).write_bytes(b + b)       # the same image in both slots
    bench["romfs"] = _ciphered(a, _csize(a)) + _ciphered(b, _csize(b))

    def sh(argv, **k):
        bench["ran"].append(argv)
        with open(argv[-1], "wb") as f:
            f.write(bench["romfs"] if argv[argv.index("-a") + 1] == "3" else bench["dump"])
        return 0, ""
    monkeypatch.setattr(ota_cycle, "sh", sh)
    ok, report = ota_cycle.check_at_rest(BOARD)
    assert ok and report["problems"] == [] and [n["slot"] for n in report["slots"]] == ["A", "B"]
    assert len(bench["exec"]) == 2                                      # DFU entered for each read
    assert [argv[argv.index("-a") + 1] for argv in bench["ran"]] == ["1", "3"]
    assert any("slot A: 0 of" in ln for ln in bench["logged"])
    # a slot in the clear fails the whole check
    bench["romfs"] = a + _ciphered(b, _csize(b))
    ok, report = ota_cycle.check_at_rest(BOARD)
    assert not ok and any("slot A: " in p for p in report["problems"])
    # and a romfs read that never happens is a problem of its own, the firmware verdict kept
    bench["romfs"] = None

    def sh_fail(argv, **k):
        bench["ran"].append(argv)
        if argv[argv.index("-a") + 1] == "3":
            return 74, "no DFU device"
        with open(argv[-1], "wb") as f:
            f.write(bench["dump"])
        return 0, ""
    monkeypatch.setattr(ota_cycle, "sh", sh_fail)
    monkeypatch.setattr(ota_cycle, "dfu_reset_catch", lambda *a, **k: (1, "still nothing"))
    ok, report = ota_cycle.check_at_rest(BOARD)
    assert not ok and report["same"] == 0 and "alt 3" in report["problems"][0]


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


def test_rollback_on_an_encrypting_board_shows_in_the_stub_not_in_boot_pys_fallback():
    expect, forbid = ota_cycle.scenario_markers("OPENMV_N6", "rollback")
    assert "boot.fallback" not in expect and "boot.fallback" not in forbid
    assert {"stub.slot_rejected", "stub.boot_reverted", "install.armed"} <= expect
    expect, _forbid = ota_cycle.scenario_markers("OPENMV4P", "rollback")   # a stub in the clear
    assert "boot.fallback" in expect


def test_the_ae3_runs_rollback_for_its_stubs_revert_path():
    ae3 = ota_cycle.regression_scenarios("OPENMV_AE3", ota_cycle.BOARDS["OPENMV_AE3"]["network"])
    assert ae3[:2] == ["delta", "rollback"] and "watchdog" in ae3
    assert {"stub.slot_rejected", "stub.boot_reverted"} <= \
        ota_cycle.scenario_markers("OPENMV_AE3", "rollback")[0]


def test_fw_corrupt_expects_the_stubs_repair_and_runs_where_the_harness_can_damage(monkeypatch):
    expect, forbid = ota_cycle.scenario_markers("OPENMV_N6", "fw_corrupt")
    assert {"stub.area_bad", "stub.install", "stub.record_written", "stub.install_verified",
            "stub.boot_reinstalled", "stub.fast_path", "stub.boot_normal", "boot.ready"} <= expect
    assert "stub.failed" in forbid and "install.start" in forbid
    n6 = ota_cycle.regression_scenarios("OPENMV_N6", ota_cycle.BOARDS["OPENMV_N6"]["network"])
    assert n6[-2:] == ["fw_corrupt", "blob_corrupt"]
    for board in ota_cycle.BOARDS:
        if board not in ota_cycle.FW_CORRUPT_BOARDS:
            for net in ("lan", "wifi", "file"):
                assert not {"fw_corrupt", "blob_corrupt"} & set(
                    ota_cycle.regression_scenarios(board, net)), board


def test_the_damage_is_the_stub_as_it_is_then_garbage_over_the_first_firmware_block(bench, monkeypatch):
    wrote = {}

    def sh(argv, **k):
        bench["ran"].append(argv)
        with open(argv[-1], "rb") as f:
            wrote["image"] = f.read()
        return 0, ""

    monkeypatch.setattr(ota_cycle, "sh", sh)
    ota_cycle.corrupt_firmware_area(BOARD)
    (argv,) = bench["ran"]
    assert argv[:7] == ["DFU", "-w", "-d", ",37c5:9206", "-a", "1", "--reset"] and argv[-2] == "-D"
    image = wrote["image"]
    assert image[:len(STUB)] == STUB and set(image[len(STUB):START]) == {0xFF}
    assert len(image) == START + 64 * 1024 and image[START:] != FW[:64 * 1024]
    assert bench["exec"] == ["import machine; machine.bootloader()"]
    # no route into DFU: the scenario stops there rather than scoring an undamaged board
    monkeypatch.setattr(ota_cycle, "sh", lambda argv, **k: (74, "no device"))
    monkeypatch.setattr(ota_cycle, "dfu_reset_catch", lambda *a, **k: (1, "still none"))
    with pytest.raises(RuntimeError, match="could not damage the board"):
        ota_cycle.corrupt_firmware_area(BOARD)


def test_blob_corrupt_takes_an_update_then_expects_the_blob_refused_and_the_other_slot_installed():
    spec = ota_cycle.SCENARIOS["blob_corrupt"]
    assert spec["publish"] == "delta" and spec["end"] == "promoted"      # two slots that verify
    then = spec["then"]
    assert then["publish"] == "none" and then["corrupt_firmware"] and then["corrupt_blob"]
    # phase 1 is an ordinary update: the two lines stay forbidden there
    expect, forbid = ota_cycle.scenario_markers("OPENMV_N6", "blob_corrupt")
    assert "confirm.promoted" in expect and {"stub.blob_bad", "stub.install_failed"} <= forbid
    # phase 2 is the repair
    expect, forbid = ota_cycle.scenario_markers("OPENMV_N6", "blob_corrupt", then=True)
    assert {"stub.area_bad", "stub.blob_bad", "stub.install_failed", "stub.install",
            "stub.record_written", "stub.install_verified", "stub.crypt",
            "stub.boot_repaired", "stub.journal", "boot.ready", "run.checkin"} <= expect
    assert "stub.boot_normal" not in expect                # every boot off the damaged slot is a repair
    assert "stub.boot_reinstalled" not in expect           # the other slot's install is a repair
    assert "stub.failed" in forbid and not {"stub.blob_bad", "stub.install_failed"} & forbid
    assert "stub.slot_clear_dropped" in forbid             # nothing in the clear turns up
    # ...and everywhere else those two lines stay forbidden
    for name in ("delta", "rollback", "fw_corrupt", "bad_sig"):
        assert {"stub.blob_bad", "stub.install_failed"} <= ota_cycle.scenario_markers("OPENMV_N6", name)[1]
    assert "stub.blob_bad" not in ota_cycle.scenario_markers("OPENMV_N6", "no_slot")[1]


def _two_slots(tmp_path, monkeypatch, blob=(0x130000, 900000)):
    import gzip
    import openmv_ota.ota.trailer as trailer_mod
    from types import SimpleNamespace
    slot = 4 * 1024 * 1024
    rnd = random.Random(3)
    update = bytes(rnd.choices(range(256), k=slot))
    factory = bytes(rnd.choices(range(256), k=2 * slot))
    with gzip.open(tmp_path / "build" / ("%s-ota.img.gz" % BOARD), "wb") as f:
        f.write(update)
    (tmp_path / "build" / ("%s-factory-romfs.img" % BOARD)).write_bytes(factory)
    monkeypatch.setattr(trailer_mod, "parse_trailer", lambda data: SimpleNamespace(
        components=[SimpleNamespace(offset=blob[0], size=blob[1])]))
    return slot, update, factory


def test_the_blob_damage_erases_one_block_of_the_blob_in_the_slot_that_holds_the_update(
        bench, monkeypatch, tmp_path):
    slot, update, factory = _two_slots(tmp_path, monkeypatch)
    ran = []

    def device_exec(code, **k):
        ran.append(code)
        if "TRAILER" in code:                   # the update is in slot B; A holds the factory image
            return 0, "noise\nTRAILER 0 %d %s\nTRAILER 1 %d %s\n" % (
                slot, factory[slot - 4096:slot].hex(), slot, update[slot - 4096:].hex())
        return 0, "ERASED 0\n"

    monkeypatch.setattr(ota_cycle, "device_exec", device_exec)
    index, at = ota_cycle.damage_booted_blob(BOARD)
    assert index == 1 and at == 0x190000                     # mid-blob, on an erase block
    assert 0x130000 <= at and at + 65536 <= 0x130000 + 900000
    assert "rom_ioctl(3, 0, %d, 4096)" % (slot + at) in ran[1]


def test_the_blob_damage_stops_when_it_cannot_be_done_right(bench, monkeypatch, tmp_path):
    with pytest.raises(RuntimeError, match="no published update"):
        ota_cycle.damage_booted_blob(BOARD)
    slot, update, factory = _two_slots(tmp_path, monkeypatch)
    trailers = "TRAILER 0 %d %s\n" % (slot, update[slot - 4096:].hex())
    monkeypatch.setattr(ota_cycle, "device_exec", lambda code, **k: (0, "TRAILER 0 %d %s\n" % (
        slot, factory[slot - 4096:slot].hex())))
    with pytest.raises(RuntimeError, match="neither slot holds the published update"):
        ota_cycle.damage_booted_blob(BOARD)
    monkeypatch.setattr(ota_cycle, "device_exec", lambda code, **k: (
        (0, trailers) if "TRAILER" in code else (1, "OSError")))
    with pytest.raises(RuntimeError, match="could not erase a block"):
        ota_cycle.damage_booted_blob(BOARD)
    _two_slots(tmp_path, monkeypatch, blob=(0x130000, 60000))
    with pytest.raises(RuntimeError, match="too small to damage safely"):
        ota_cycle.damage_booted_blob(BOARD)


def test_blob_corrupt_damages_the_blob_before_the_firmware_area(bench, monkeypatch):
    order = []
    monkeypatch.setattr(ota_cycle, "damage_booted_blob", lambda board: order.append("blob"))
    monkeypatch.setattr(ota_cycle, "sh", lambda argv, **k: (order.append(argv), (0, ""))[1])
    ota_cycle.corrupt_firmware_area(BOARD, blob=True)
    assert order[0] == "blob" and len(order) == 2            # then one download, to the firmware
    assert order[1][order[1].index("-a") + 1] == "1" and "--reset" in order[1]


def test_no_slot_on_a_board_that_encrypts_expects_the_stubs_last_resort(monkeypatch):
    n6 = ota_cycle.regression_scenarios("OPENMV_N6", ota_cycle.BOARDS["OPENMV_N6"]["network"])
    assert "no_slot" in n6 and n6.index("no_slot") < n6.index("watchdog")   # before an armed app
    expect, forbid = ota_cycle.scenario_markers("OPENMV_N6", "no_slot")
    assert {"stub.version", "stub.slot_rejected", "stub.no_slot_boot", "boot.no_slot"} <= expect
    assert "stub.failed" not in forbid
    # a board bricked by erasing its slots alone (no firmware over USB) keeps the area its stub
    # installed: it is not asked for the line, and nowhere else may the line appear
    monkeypatch.setattr(ota_cycle, "_has_stub", lambda b: b in ("OPENMV_N6", "OPENMV_RT1060"))
    assert ota_cycle._stub_encrypts("OPENMV_RT1060") and not ota_cycle._usb_firmware("OPENMV_RT1060")
    assert "stub.no_slot_boot" not in ota_cycle.scenario_markers("OPENMV_RT1060", "no_slot")[0]
    assert "stub.crypt" in ota_cycle.scenario_markers("OPENMV_RT1060", "delta")[0]
    assert "no_slot_secure" not in ota_cycle.regression_scenarios("OPENMV_RT1060", "lan")
    assert "stub.no_slot_boot" in ota_cycle.scenario_markers("OPENMV_N6", "delta")[1]


def test_no_slot_secure_expects_the_usb_firmware_refused_not_booted():
    """The same brick under a stub built for secure boot: the stub stops, boot.py never runs."""
    n6 = ota_cycle.regression_scenarios("OPENMV_N6", ota_cycle.BOARDS["OPENMV_N6"]["network"])
    assert n6[n6.index("no_slot") - 1] == "no_slot_secure"      # it leaves a plain app behind
    assert n6[n6.index("no_slot_secure") - 1] == "reinstall"    # ...and inherits one
    assert "no_slot_secure" not in ota_cycle.regression_scenarios("OPENMV_RT1060", "lan")
    expect, forbid = ota_cycle.scenario_markers("OPENMV_N6", "no_slot_secure")
    assert expect == {"stub.version", "stub.slot_rejected", "stub.unsigned_refused"}
    assert {"stub.no_slot_boot", "boot.no_slot", "run.checkin"} <= forbid
    # ...and the refusal is forbidden wherever the stub is built as usual
    for key in ("no_slot", "delta", "fw_corrupt"):
        assert "stub.unsigned_refused" in ota_cycle.scenario_markers("OPENMV_N6", key)[1]


def test_set_secure_boot_edits_the_project_and_rebuilds_the_firmware(tmp_path, monkeypatch):
    toml = tmp_path / "openmv-ota.toml"
    toml.write_text('[product]\nname = "p"\n[ota]\nenabled = true\n')
    monkeypatch.setitem(ota_cycle.CFG, "project", str(tmp_path))
    monkeypatch.setitem(ota_cycle.CFG, "sdk", "/sdk")
    runs = []
    envs = []
    monkeypatch.setattr(ota_cycle.subprocess, "run",
                        lambda argv, **kw: (runs.append(argv), envs.append(kw["env"])))
    ota_cycle.set_secure_boot("OPENMV_N6", True)
    ota_cycle.set_secure_boot("OPENMV_N6", True)                # twice is still one line
    assert toml.read_text().count('secure_boot = "always"\n') == 1
    assert '[ota]\nsecure_boot = "always"\n' in toml.read_text()
    ota_cycle.set_secure_boot("OPENMV_N6", False)
    assert toml.read_text() == '[product]\nname = "p"\n[ota]\nenabled = true\n'
    assert len(runs) == 3 and all(r[1:3] == ["build", "firmware"] and "OPENMV_N6" in r for r in runs)
    # each build opens the stub's bench UART, or a stub with no slot to read one from is silent
    assert all(e["STUB_DEBUG_UART"] == str(ota_cycle.BOARDS["OPENMV_N6"]["cov_uart"]) for e in envs)
    toml.write_text('[product]\nname = "p"\n')
    with pytest.raises(RuntimeError, match="no single"):
        ota_cycle.set_secure_boot("OPENMV_N6", True)


def test_the_dfu_brick_erases_both_slots_ends_then_flashes_the_firmware(bench, monkeypatch):
    ran = []
    monkeypatch.setattr(ota_cycle, "_has_stub", lambda b: b == BOARD)
    monkeypatch.setattr(ota_cycle, "device_exec",
                        lambda code, **k: bench["exec"].append(code) or (0, "ERASED 0 0 0\nERASED 1 0 0\n"))
    monkeypatch.setattr(ota_cycle, "sh", lambda argv, **k: ran.append(argv) or (0, ""))
    monkeypatch.setattr(ota_cycle, "_log_cli_tail", lambda *a, **k: None)
    ota_cycle._brick_slots_dfu(BOARD)
    assert "rom_ioctl(3, 0, s * n, 4096)" in bench["exec"][0]
    assert "(s + 1) * n - 4096" in bench["exec"][0]
    (argv,) = ran
    assert argv[1:3] == ["flash", "firmware"] and argv[argv.index("-b") + 1] == BOARD
    compile(ota_cycle._ERASE_TRAILERS, "erase", "exec")
    # the erase did not happen: nothing is flashed over a board whose slots still verify
    monkeypatch.setattr(ota_cycle, "device_exec", lambda code, **k: (1, "Traceback"))
    with pytest.raises(RuntimeError, match="could not erase the slots' trailers"):
        ota_cycle._brick_slots_dfu(BOARD)
    assert len(ran) == 1
    with pytest.raises(RuntimeError, match="not implemented for OPENMV_AE3"):
        monkeypatch.setattr(ota_cycle, "_has_stub", lambda b: False)
        ota_cycle._brick_slots_dfu("OPENMV_AE3")


def test_a_camera_stopped_in_its_stub_is_flashed_through_the_bootloaders_window(monkeypatch):
    calls = []
    monkeypatch.setitem(ota_cycle.CFG, "project", "/proj")
    monkeypatch.setitem(ota_cycle.CFG, "sdk", "/sdk")
    monkeypatch.setitem(ota_cycle.CFG, "dfu", "/dfu")
    monkeypatch.setattr(ota_cycle, "_register_args", lambda: [])
    monkeypatch.setattr(ota_cycle, "_log_cli_tail", lambda rc, out: None)
    monkeypatch.setattr(ota_cycle, "flash_golden", lambda board: calls.append(("usual", board)))
    rcs = iter([0, 2])
    monkeypatch.setattr(ota_cycle, "dfu_reset_catch",
                        lambda board, argv, timeout: (calls.append(("catch", argv)), (next(rcs), ""))[1])
    ota_cycle.flash_golden_in_bootloader("OPENMV_N6")
    assert [c[0] for c in calls] == ["catch"]
    assert calls[0][1][1:3] == ["flash", "factory"] and calls[0][1][-1] == "--in-bootloader"
    ota_cycle.flash_golden_in_bootloader("OPENMV_N6")           # the window was missed
    assert [c[0] for c in calls] == ["catch", "catch", "usual"]

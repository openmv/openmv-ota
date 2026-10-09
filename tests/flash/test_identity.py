"""The last step of ``flash factory`` on a board with keys. Booting its new factory image, the
camera makes its keys if it has none (boot.py, before the app runs) and, for a short window,
answers an ``OMVKEYS <challenge>`` line on its console with both public keys and a signature over
the challenge. The tool sends the request until it is answered, verifies the signature against
the identity key, and prints both keys -- never breaking into a running app."""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed

from openmv_ota.cli import main
from openmv_ota.flash import flash as fl
from openmv_ota.flash.device import Camera
from openmv_ota.flash.errors import FlashError

KEY = ec.generate_private_key(ec.SECP256R1())
PUB = KEY.public_key().public_bytes(serialization.Encoding.X962,
                                    serialization.PublicFormat.UncompressedPoint)
KX = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
    serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


class _Console:
    """The camera's console: silent for ``boot`` reads (still verifying its image), then
    ``answer(challenge)``'s line, delivered across two reads like a real USB stream."""

    def __init__(self, answer, boot=2):
        self.answer, self.boot, self.sent, self.closed, self.out = answer, boot, [], False, None

    def write(self, data):
        self.sent.append(data)

    def read(self, n):
        if self.boot:
            self.boot -= 1
            return b""
        if self.out is None:
            request = self.sent[-1].decode()
            assert request.startswith("OMVKEYS ") and request.endswith("\r\n")
            self.out = self.answer(bytes.fromhex(request.split()[1])).encode()
        chunk, self.out = self.out[:20], self.out[20:]
        return chunk

    def close(self):
        self.closed = True


def _signed(made=1, key=KEY):
    def answer(digest):
        sig = key.sign(digest, ec.ECDSA(Prehashed(hashes.SHA256())))
        return "boot noise\r\nSE-ID %s %s %s %d\r\n" % (PUB.hex(), KX.hex(), sig.hex(), made)
    return answer


@pytest.fixture
def camera(monkeypatch):
    """The camera re-enumerates on the second look; ``set(answer)`` gives its console."""
    looks = iter([None, Camera("/dev/ttyACM9", "SN1")])
    monkeypatch.setattr(fl.device, "select", lambda raw, serial: next(looks))
    monkeypatch.setattr(fl.time, "sleep", lambda s: None)
    box = {}

    def set_answer(answer, **kw):
        box["con"] = _Console(answer, **kw)
        monkeypatch.setattr(fl, "_open_console", lambda port: box["con"])
        return box["con"]
    return set_answer


def test_a_board_without_keys_has_nothing_to_provision():
    assert fl.provision_keys(board="OPENMV_AE3") == []


def test_dry_run_shows_the_request():
    (step,) = fl.provision_keys(board="ARDUINO_GIGA", dry_run=True)
    assert step.argv[0] == "send" and step.argv[1].startswith("'OMVKEYS ")


@pytest.mark.parametrize("made,word", [(1, "made"), (0, "present")])
def test_the_camera_s_signature_is_checked_and_its_keys_reported(camera, made, word):
    con = camera(_signed(made))
    (step,) = fl.provision_keys(board="OPENMV4")
    assert step.label == "keys %s: identity %s, exchange %s" % (word, PUB.hex(), KX.hex())
    assert len(con.sent) >= 3 and len(set(con.sent)) == 1 and con.closed   # repeated, then shut


def test_a_giga_says_its_chip_was_configured(camera, capsys):
    camera(_signed(1))
    fl.provision_keys(board="ARDUINO_GIGA")
    assert "ATECC608 the Arduino-compatible way (one-time)" in capsys.readouterr().err


def test_a_signature_that_does_not_verify_fails_the_flash(camera):
    camera(lambda d: _signed()(bytes(32)))                 # signed something else
    with pytest.raises(FlashError, match="does not verify"):
        fl.provision_keys(board="OPENMV_RT1060")


def test_a_key_that_is_not_a_curve_point_fails_the_flash(camera):
    camera(lambda d: "SE-ID 04%s %s 3006020101020101 0\r\n" % ("00" * 64, KX.hex()))
    with pytest.raises(FlashError, match="does not verify"):
        fl.provision_keys(board="OPENMV_RT1060")


def test_damaged_keys_fail_the_flash_with_the_camera_s_words(camera):
    camera(lambda d: "SE-ERROR key store: the camera's newest key record is damaged\r\n")
    with pytest.raises(FlashError, match="newest key record is damaged"):
        fl.provision_keys(board="OPENMV4")


def test_a_camera_whose_romfs_says_it_has_no_keys_is_fine(camera):
    camera(lambda d: "SE-NONE\r\n")
    assert fl.provision_keys(board="OPENMV4") == []


def test_a_camera_that_never_answers_fails_the_flash(camera, monkeypatch):
    camera(_signed(), boot=10 ** 6)
    clock = iter(range(0, 10_000, 1))
    monkeypatch.setattr(fl.time, "monotonic", lambda: next(clock))
    with pytest.raises(FlashError, match="did not answer for its keys"):
        fl.provision_keys(board="OPENMV4")


def test_a_camera_that_never_comes_back_fails_the_flash(monkeypatch):
    clock = iter(range(0, 10_000, 30))
    monkeypatch.setattr(fl.device, "select", lambda raw, serial: None)
    monkeypatch.setattr(fl.time, "sleep", lambda s: None)
    monkeypatch.setattr(fl.time, "monotonic", lambda: next(clock))
    with pytest.raises(FlashError, match="did not come back"):
        fl.provision_keys(board="ARDUINO_NICLA_VISION")


def test_open_console_is_a_serial_port(monkeypatch):
    import serial
    seen = {}
    monkeypatch.setattr(serial, "Serial", lambda port, baud, timeout: seen.update(p=port) or "S")
    assert fl._open_console("/dev/ttyACM3") == "S" and seen["p"] == "/dev/ttyACM3"


def test_flash_factory_ends_by_provisioning_and_firmware_romfs_do_not(monkeypatch, tmp_path,
                                                                     capsys):
    calls = []
    monkeypatch.setattr(fl, "provision_keys", lambda **k: calls.append(k) or [
        fl.IdentityStep("keys made: identity 04ab, exchange 04cd", ["port"])])
    for verb in ("factory", "firmware", "romfs"):
        monkeypatch.setattr(fl, "flash_" + verb, lambda *a, **k: [])
        assert main(["flash", verb, str(tmp_path), "-b", "ARDUINO_GIGA"]) == 0
    assert len(calls) == 1
    assert "keys made: identity 04ab, exchange 04cd (ARDUINO_GIGA)" in capsys.readouterr().out


def test_a_failed_provisioning_fails_the_flash(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(fl, "flash_factory", lambda *a, **k: [])

    def fail(**k):
        raise FlashError("the camera's keys: damaged")
    monkeypatch.setattr(fl, "provision_keys", fail)
    assert main(["flash", "factory", str(tmp_path), "-b", "ARDUINO_GIGA"]) != 0
    assert "the camera's keys: damaged" in capsys.readouterr().err


class _MissedWindow(_Console):
    """A camera whose boot closed its key window before the console opened: silent until it is
    soft-reset (Ctrl-C out of the app, Ctrl-D at the REPL), then it answers like any boot."""

    def read(self, n):
        if b"\x04" not in self.sent:
            return b""
        return super().read(n)


def test_a_missed_window_soft_resets_the_camera_with_the_console_held(camera, monkeypatch):
    con = camera(_signed(0))
    con.__class__ = _MissedWindow
    clock = (i / 10 for i in range(100_000))                 # 0.1 s a look
    monkeypatch.setattr(fl.time, "monotonic", lambda: next(clock))
    (step,) = fl.provision_keys(board="ARDUINO_GIGA")
    assert step.label.startswith("keys present")
    keys = [d for d in con.sent if not d.startswith(b"OMVKEYS")]
    assert keys == list(fl._SOFT_RESET)                     # once, Ctrl-C x3 then Ctrl-D

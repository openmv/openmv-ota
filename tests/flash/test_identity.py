"""The last step of every ``flash`` that leaves a camera on OTA firmware + romfs, on a board with
keys: the camera, back on its new firmware, provisions its keys (``se.provision()``: made if it
has none, never replaced) and signs the host's challenge; the host checks that signature against
the identity key it reports, and prints both keys."""

from __future__ import annotations

import re

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


def _challenge(argv):
    return bytes.fromhex(re.search(r"unhexlify\('([0-9a-f]{64})'\)", argv[-1]).group(1))


def _camera(answer):
    """A camera whose `mpremote exec` answers with ``answer(digest) -> (rc, output)``."""
    seen = []

    def run_quiet(argv):
        seen.append(argv)
        return answer(_challenge(argv))
    return seen, run_quiet


def _signed(digest, made=1, key=KEY):
    sig = key.sign(digest, ec.ECDSA(Prehashed(hashes.SHA256())))
    return 0, "noise\r\nSE-ID %s %s %s %d\r\n" % (PUB.hex(), KX.hex(), sig.hex(), made)


@pytest.fixture
def back(monkeypatch):
    """The camera re-enumerates on the second look."""
    looks = iter([None, Camera("/dev/ttyACM9", "SN1")])
    monkeypatch.setattr(fl.device, "select", lambda raw, serial: next(looks))
    monkeypatch.setattr(fl.time, "sleep", lambda s: None)


def test_a_board_without_keys_has_nothing_to_provision():
    assert fl.provision_keys(board="OPENMV_AE3") == []


def test_dry_run_shows_the_command():
    (step,) = fl.provision_keys(board="ARDUINO_GIGA", dry_run=True, mpremote="/x/mpremote")
    assert step.argv[:4] == ["/x/mpremote", "connect", "<camera>", "exec"]
    assert "se.provision()" in step.argv[-1] and len(_challenge(step.argv)) == 32


@pytest.mark.parametrize("made,word", [(1, "made"), (0, "present")])
def test_the_camera_s_signature_is_checked_and_its_keys_reported(back, monkeypatch, made, word):
    seen, run_quiet = _camera(lambda d: _signed(d, made))
    monkeypatch.setattr(fl.runner, "run_quiet", run_quiet)
    (step,) = fl.provision_keys(board="OPENMV4", mpremote="mp")
    assert step.label == "keys %s: identity %s, exchange %s" % (word, PUB.hex(), KX.hex())
    assert seen[0][:3] == ["mp", "connect", "/dev/ttyACM9"]


def test_a_camera_refusing_the_raw_repl_after_a_flash_is_asked_again_without_a_reset(
        back, monkeypatch):
    answers = iter([lambda d: (1, "TransportError: could not enter raw repl"), _signed])
    seen, run_quiet = _camera(lambda d: next(answers)(d))
    monkeypatch.setattr(fl.runner, "run_quiet", run_quiet)
    (step,) = fl.provision_keys(board="OPENMV3", mpremote="mp")
    assert "resume" not in seen[0] and seen[1][3:5] == ["resume", "exec"]
    assert step.label.startswith("keys made")


def test_a_giga_says_its_chip_was_configured(back, monkeypatch, capsys):
    monkeypatch.setattr(fl.runner, "run_quiet", _camera(lambda d: _signed(d, 1))[1])
    fl.provision_keys(board="ARDUINO_GIGA")
    assert "ATECC608 the Arduino-compatible way (one-time)" in capsys.readouterr().err


def test_a_signature_that_does_not_verify_fails_the_flash(back, monkeypatch):
    _, run_quiet = _camera(lambda d: _signed(bytes(32)))    # signed something else
    monkeypatch.setattr(fl.runner, "run_quiet", run_quiet)
    with pytest.raises(FlashError, match="does not verify"):
        fl.provision_keys(board="OPENMV_RT1060")


def test_a_key_that_is_not_a_curve_point_fails_the_flash(back, monkeypatch):
    answer = "SE-ID 04%s %s 3006020101020101 0" % ("00" * 64, KX.hex())
    monkeypatch.setattr(fl.runner, "run_quiet", _camera(lambda d: (0, answer))[1])
    with pytest.raises(FlashError, match="does not verify"):
        fl.provision_keys(board="OPENMV_RT1060")


def test_damaged_keys_fail_the_flash_with_the_camera_s_words(back, monkeypatch):
    answer = (1, "OSError: key store: the camera's newest key record is damaged")
    monkeypatch.setattr(fl.runner, "run_quiet", _camera(lambda d: answer)[1])
    with pytest.raises(FlashError, match="newest key record is damaged"):
        fl.provision_keys(board="OPENMV4", required=False)    # never skipped, even then


def test_a_romfs_without_the_keys_code_is_skipped_only_when_not_required(back, monkeypatch,
                                                                         capsys):
    answer = (1, "ImportError: no module named 'openmv_ota.se'")
    monkeypatch.setattr(fl.runner, "run_quiet", _camera(lambda d: answer)[1])
    assert fl.provision_keys(board="OPENMV4", required=False) == []
    assert "has no openmv_ota.se yet" in capsys.readouterr().err


def test_a_romfs_without_the_keys_code_fails_a_factory_flash(back, monkeypatch):
    answer = (1, "ImportError: no module named 'openmv_ota.se'")
    monkeypatch.setattr(fl.runner, "run_quiet", _camera(lambda d: answer)[1])
    with pytest.raises(FlashError, match="provisioning the camera's keys failed"):
        fl.provision_keys(board="OPENMV4")


def test_a_camera_that_never_comes_back_fails_the_flash(monkeypatch):
    clock = iter(range(0, 10_000, 30))
    monkeypatch.setattr(fl.device, "select", lambda raw, serial: None)
    monkeypatch.setattr(fl.time, "sleep", lambda s: None)
    monkeypatch.setattr(fl.time, "monotonic", lambda: next(clock))
    with pytest.raises(FlashError, match="did not come back"):
        fl.provision_keys(board="ARDUINO_NICLA_VISION")


@pytest.mark.parametrize("verb,required", [("factory", True), ("firmware", False),
                                           ("romfs", False)])
def test_every_flash_ends_by_provisioning(monkeypatch, tmp_path, capsys, verb, required):
    monkeypatch.setattr(fl, "flash_" + verb, lambda *a, **k: [])
    seen = {}

    def provision(**k):
        seen.update(k)
        return [fl.IdentityStep("keys made: identity 04ab, exchange 04cd", ["mp"])]
    monkeypatch.setattr(fl, "provision_keys", provision)
    assert main(["flash", verb, str(tmp_path), "-b", "ARDUINO_GIGA"]) == 0
    assert seen.get("required", True) is required
    assert "keys made: identity 04ab, exchange 04cd (ARDUINO_GIGA)" in capsys.readouterr().out


def test_a_failed_provisioning_fails_the_flash(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(fl, "flash_factory", lambda *a, **k: [])

    def fail(**k):
        raise FlashError("provisioning the camera's keys failed:\nboom")
    monkeypatch.setattr(fl, "provision_keys", fail)
    assert main(["flash", "factory", str(tmp_path), "-b", "ARDUINO_GIGA"]) != 0
    assert "provisioning the camera's keys failed" in capsys.readouterr().err

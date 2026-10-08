"""``flash factory``'s last step on a board with a secure element: the camera, back on its new
firmware, opens its secure element (provisioning a blank ATECC608) and signs the host's challenge,
and the host checks that signature against the key it reports."""

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


def _challenge(argv):
    return bytes.fromhex(re.search(r"unhexlify\('([0-9a-f]{64})'\)", argv[-1]).group(1))


def _camera(answer):
    """A camera whose `mpremote exec` answers with ``answer(digest) -> (rc, output)``."""
    seen = []

    def run_quiet(argv):
        seen.append(argv)
        return answer(_challenge(argv))
    return seen, run_quiet


def _signed(digest):
    sig = KEY.sign(digest, ec.ECDSA(Prehashed(hashes.SHA256())))
    return 0, "noise\r\nSE-ID %s %s\r\n" % (PUB.hex(), sig.hex())


@pytest.fixture
def back(monkeypatch):
    """The camera re-enumerates on the second look."""
    looks = iter([None, Camera("/dev/ttyACM9", "SN1")])
    monkeypatch.setattr(fl.device, "select", lambda raw, serial: next(looks))
    monkeypatch.setattr(fl.time, "sleep", lambda s: None)


def test_a_board_without_keys_has_nothing_to_check():
    assert fl.factory_identity(board="OPENMV_N6") == []


def test_dry_run_shows_the_command():
    (step,) = fl.factory_identity(board="ARDUINO_GIGA", dry_run=True, mpremote="/x/mpremote")
    assert step.argv[:4] == ["/x/mpremote", "connect", "<camera>", "exec"]
    assert len(_challenge(step.argv)) == 32


def test_the_camera_s_signature_is_checked_and_its_key_reported(back, monkeypatch):
    seen, run_quiet = _camera(_signed)
    monkeypatch.setattr(fl.runner, "run_quiet", run_quiet)
    (step,) = fl.factory_identity(board="ARDUINO_GIGA", mpremote="mp")
    assert step.label == "secure element key %s" % PUB.hex()
    assert seen[0][:3] == ["mp", "connect", "/dev/ttyACM9"]


def test_a_signature_that_does_not_verify_fails_the_factory(back, monkeypatch):
    _, run_quiet = _camera(lambda d: _signed(bytes(32)))    # signed something else
    monkeypatch.setattr(fl.runner, "run_quiet", run_quiet)
    with pytest.raises(FlashError, match="does not verify"):
        fl.factory_identity(board="OPENMV_RT1060")


def test_a_key_that_is_not_a_curve_point_fails_the_factory(back, monkeypatch):
    _, run_quiet = _camera(lambda d: (0, "SE-ID 04%s 3006020101020101" % ("00" * 64)))
    monkeypatch.setattr(fl.runner, "run_quiet", run_quiet)
    with pytest.raises(FlashError, match="does not verify"):
        fl.factory_identity(board="OPENMV_RT1060")


def test_a_camera_that_errors_fails_the_factory_with_its_output(back, monkeypatch):
    _, run_quiet = _camera(lambda d: (1, "OSError: atecc608: not Arduino's configuration"))
    monkeypatch.setattr(fl.runner, "run_quiet", run_quiet)
    with pytest.raises(FlashError, match="not Arduino's configuration"):
        fl.factory_identity(board="ARDUINO_GIGA")


def test_a_camera_that_never_comes_back_fails_the_factory(monkeypatch):
    clock = iter(range(0, 10_000, 30))
    monkeypatch.setattr(fl.device, "select", lambda raw, serial: None)
    monkeypatch.setattr(fl.time, "sleep", lambda s: None)
    monkeypatch.setattr(fl.time, "monotonic", lambda: next(clock))
    with pytest.raises(FlashError, match="did not come back"):
        fl.factory_identity(board="ARDUINO_NICLA_VISION")


def test_flash_factory_reports_the_key(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(fl, "flash_factory", lambda *a, **k: [])
    monkeypatch.setattr(fl, "factory_identity",
                        lambda **k: [fl.IdentityStep("secure element key 04ab", ["mp"])])
    assert main(["flash", "factory", str(tmp_path), "-b", "ARDUINO_GIGA"]) == 0
    assert "secure element key 04ab (ARDUINO_GIGA)" in capsys.readouterr().out


def test_a_failed_check_fails_flash_factory(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(fl, "flash_factory", lambda *a, **k: [])

    def fail(**k):
        raise FlashError("secure element check failed on the camera:\nboom")
    monkeypatch.setattr(fl, "factory_identity", fail)
    assert main(["flash", "factory", str(tmp_path), "-b", "ARDUINO_GIGA"]) != 0
    assert "secure element check failed" in capsys.readouterr().err

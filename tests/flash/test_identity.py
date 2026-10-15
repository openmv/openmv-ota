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


def _signed(made=1, key=KEY, device_id="0036324E4236"):
    def answer(digest):
        sig = key.sign(digest, ec.ECDSA(Prehashed(hashes.SHA256())))
        return "boot noise\r\nSE-ID %s %s %s %d %s\r\n" % (PUB.hex(), KX.hex(), sig.hex(), made,
                                                         device_id)
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
    assert fl.provision_keys(board="ARDUINO_NANO_RP2040_CONNECT") == []


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
    monkeypatch.setattr(serial, "Serial",
                        lambda port, baud, timeout, write_timeout: seen.update(p=port) or "S")
    assert fl._open_console("/dev/ttyACM3") == "S" and seen["p"] == "/dev/ttyACM3"


def test_a_port_udev_hasnt_handed_over_yet_is_opened_on_a_retry(camera, monkeypatch):
    """Back on USB before udev gives its port the dialout group: the first opens are EACCES."""
    con = camera(_signed())
    tries = iter([OSError(13, "Permission denied"), OSError(13, "Permission denied"), con])

    def open_console(port):
        got = next(tries)
        if isinstance(got, OSError):
            raise got
        return got
    monkeypatch.setattr(fl, "_open_console", open_console)
    (step,) = fl.provision_keys(board="ARDUINO_GIGA")
    assert "keys made" in step.label and con.closed


def test_a_port_that_never_opens_fails_the_flash_saying_why(camera, monkeypatch):
    camera(_signed())
    clock = iter(range(0, 1000, 2))
    monkeypatch.setattr(fl.time, "monotonic", lambda: next(clock))

    def denied(port):
        raise OSError(13, "Permission denied")
    monkeypatch.setattr(fl, "_open_console", denied)
    monkeypatch.setattr(fl.device, "select", lambda raw, serial: Camera("/dev/ttyACM9", "SN1"))
    with pytest.raises(fl.FlashError, match="dialout group"):
        fl.provision_keys(board="ARDUINO_GIGA")
    monkeypatch.setattr(fl, "_open_console", lambda port: (_ for _ in ()).throw(OSError(16, "Busy")))
    with pytest.raises(fl.FlashError, match="Busy") as e:
        fl.provision_keys(board="ARDUINO_GIGA")
    assert "dialout" not in str(e.value)


def test_flash_factory_ends_by_provisioning_and_firmware_romfs_do_not(monkeypatch, tmp_path,
                                                                     capsys):
    calls = []
    monkeypatch.setattr(fl, "provision_keys", lambda **k: calls.append(k) or [
        fl.IdentityStep("keys made: identity 04ab, exchange 04cd", ["port"])])
    for verb in ("factory", "firmware", "romfs"):
        monkeypatch.setattr(fl, "flash_" + verb, lambda *a, **k: [])
        extra = ["--no-register"] if verb == "factory" else []
        assert main(["flash", verb, str(tmp_path), "-b", "ARDUINO_GIGA", *extra]) == 0
    assert len(calls) == 1
    assert "keys made: identity 04ab, exchange 04cd (ARDUINO_GIGA)" in capsys.readouterr().out


def test_a_failed_provisioning_fails_the_flash(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(fl, "flash_factory", lambda *a, **k: [])

    def fail(**k):
        raise FlashError("the camera's keys: damaged")
    monkeypatch.setattr(fl, "provision_keys", fail)
    assert main(["flash", "factory", str(tmp_path), "-b", "ARDUINO_GIGA", "--no-register"]) != 0
    assert "the camera's keys: damaged" in capsys.readouterr().err


class _MissedWindow(_Console):
    """A camera whose boot closed its key window before the console opened: silent until it is
    soft-reset (Ctrl-C out of the app, Ctrl-D at the REPL), then it answers like any boot."""

    def read(self, n):
        if fl._SOFT_RESET[-1] not in self.sent:
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


class _Gone:
    """A console whose camera re-enumerated (a soft reset drops the N6 off USB)."""

    def __init__(self, on="read"):
        self.on, self.sent, self.closed = on, [], False

    def write(self, data):
        if self.on == "write":
            raise OSError("write failed: device disconnected")
        self.sent.append(data)

    def read(self, n):
        import serial
        raise serial.SerialException("device reports readiness to read but returned no data")

    def close(self):
        self.closed = True


@pytest.mark.parametrize("on", ["read", "write"])
def test_a_camera_that_re_enumerates_at_its_soft_reset_is_reopened(monkeypatch, on):
    """The N6's soft reset drops it off USB: the console is reopened once it is back, and its
    next boot answers."""
    gone = _Gone(on)
    back = _Console(_signed(0), boot=1)
    consoles = iter([gone, OSError("not ready"), back])

    def open_console(port):
        c = next(consoles)
        if isinstance(c, Exception):
            raise c
        return c
    looks = iter([Camera("/dev/ttyACM9", "SN1"), None, Camera("/dev/ttyACM9", "SN1"),
                  Camera("/dev/ttyACM9", "SN1")])
    monkeypatch.setattr(fl.device, "select", lambda raw, serial: next(looks))
    monkeypatch.setattr(fl.time, "sleep", lambda s: None)
    monkeypatch.setattr(fl, "_open_console", open_console)
    (step,) = fl.provision_keys(board="OPENMV_N6")
    assert step.label.startswith("keys present") and gone.closed and back.closed


def test_a_camera_that_doesnt_come_back_after_dropping_off_fails_the_flash(monkeypatch):
    class Unclosable(_Gone):
        def close(self):
            raise OSError("already gone")
    looks = iter([Camera("/dev/ttyACM9", "SN1")])          # there once, then gone for good
    monkeypatch.setattr(fl.device, "select", lambda raw, serial: next(looks, None))
    monkeypatch.setattr(fl.time, "sleep", lambda s: None)
    clock = (i / 10 for i in range(100_000))
    monkeypatch.setattr(fl.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(fl, "_open_console", lambda port: Unclosable())
    with pytest.raises(fl.FlashError, match="did not answer for its keys"):
        fl.provision_keys(board="OPENMV_N6")


def test_a_port_that_isn_t_taking_input_yet_is_asked_again(camera):
    """A camera still booting can leave its port refusing writes for a moment: that is retried,
    not taken for a dropped camera."""
    import serial
    con = camera(_signed(0))
    real_write, stalls = con.write, [2]

    def write(data):
        if stalls[0]:
            stalls[0] -= 1
            raise serial.SerialTimeoutException("Write timeout")
        real_write(data)
    con.write = write
    (step,) = fl.provision_keys(board="OPENMV_N6")
    assert step.label.startswith("keys present") and not stalls[0]


def test_an_answer_is_heard_even_while_the_port_refuses_writes(camera):
    """The camera answered and its app doesn't read input: our repeated requests fill it, and
    every write times out from then on. The answer waiting to be read is still read."""
    import serial
    con = camera(_signed(0), boot=1)
    real_write, stuck = con.write, []

    def write(data):
        if con.sent:                                     # took the first request, then full
            stuck.append(data)
            raise serial.SerialTimeoutException("Write timeout")
        real_write(data)
    con.write = write
    (step,) = fl.provision_keys(board="OPENMV_N6")
    assert step.label.startswith("keys present") and stuck


def test_an_answer_ending_in_extra_carriage_returns_is_a_line(camera):
    """A stdout that turns "\n" into "\r\n" turns a line already ending "\r\n" into "\r\r\n"."""
    camera(lambda digest: _signed(0)(digest).replace("\r\n", "\r\r\n") + "more output\r\n")
    (step,) = fl.provision_keys(board="OPENMV2")
    assert step.label.startswith("keys present")


def test_an_answer_missing_only_its_newline_is_taken_once_the_port_goes_quiet(camera):
    """An older boot.py print()ed the answer, and the N6 started its app before sending the
    newline: the whole line, short of its end, and nothing more."""
    def unterminated(digest):
        return _signed(0)(digest).rstrip("\r\n")
    camera(unterminated)
    (step,) = fl.provision_keys(board="OPENMV_N6")
    assert step.label.startswith("keys present")


def test_an_answer_short_of_its_newline_survives_the_camera_dropping_off(monkeypatch):
    """The N6's watchdog bit a few seconds after it answered: the port went with the newline
    still unsent. What was heard before it went is the answer."""
    class AnswersThenGoes(_Console):
        def read(self, n):
            if self.out == b"":
                raise OSError("device disconnected")
            return super().read(n)
    def unterminated(digest):
        return _signed(0)(digest).rstrip("\r\n")
    con = AnswersThenGoes(unterminated, boot=0)
    monkeypatch.setattr(fl.device, "select", lambda raw, serial: Camera("/dev/ttyACM9", "SN1"))
    monkeypatch.setattr(fl.time, "sleep", lambda s: None)
    monkeypatch.setattr(fl, "_open_console", lambda port: con)
    (step,) = fl.provision_keys(board="OPENMV_N6")
    assert step.label.startswith("keys present")


def test_a_cut_off_answer_is_not_taken(camera, monkeypatch):
    def cut(digest):
        return _signed(0)(digest)[:120]
    camera(cut)
    clock = (i / 10 for i in range(100_000))
    monkeypatch.setattr(fl.time, "monotonic", lambda: next(clock))
    with pytest.raises(fl.FlashError, match="did not answer"):
        fl.provision_keys(board="OPENMV_N6")


def test_the_console_bounds_its_writes(monkeypatch):
    seen = {}
    monkeypatch.setattr("serial.Serial", lambda port, baud, **kw: seen.update(kw) or "S")
    fl._open_console("/dev/ttyACM0")
    assert seen["write_timeout"] == 1


def test_a_camera_that_keeps_dropping_off_fails_the_flash(monkeypatch, capsys):
    """Each drop is reported and survived, a few times; a camera that never holds its port
    fails the flash rather than looping."""
    opened = []
    monkeypatch.setattr(fl.device, "select", lambda raw, serial: Camera("/dev/ttyACM9", "SN1"))
    monkeypatch.setattr(fl.time, "sleep", lambda s: None)
    monkeypatch.setattr(fl, "_open_console", lambda port: opened.append(1) or _Gone())
    with pytest.raises(fl.FlashError, match="did not answer for its keys"):
        fl.provision_keys(board="OPENMV_N6")
    assert len(opened) == 1 + fl._IDENTITY_RECONNECTS
    assert capsys.readouterr().err.count("dropped off USB") == 1 + fl._IDENTITY_RECONNECTS


# --- registering the keys with the server -----------------------------------------------------

def test_the_keys_are_registered_under_the_camera_s_own_id(camera):
    camera(_signed(0))
    seen = []

    def registrar(board, device_id, identity, exchange):
        seen.append((board, device_id, identity, exchange))
        return {"device_id": "%s:%s" % (board, device_id), "account_id": "acctA",
                "fingerprint": "ab" * 32, "rekeyed": False}
    keys, reg_step = fl.provision_keys(board="OPENMV4", registrar=registrar)
    assert seen == [("OPENMV4", "0036324E4236", PUB.hex(), KX.hex())]
    assert reg_step.label == "registered OPENMV4:0036324E4236 (key abababababababab)"
    assert reg_step.argv == ["acctA"] and keys.label.startswith("keys present")


def test_a_rekey_says_so(camera):
    camera(_signed(1))
    _keys, step = fl.provision_keys(board="OPENMV4", registrar=lambda *a: {
        "device_id": "OPENMV4:1", "account_id": "a", "fingerprint": "cd" * 32, "rekeyed": True})
    assert step.label.endswith(", replacing its old keys")


def test_a_camera_that_cant_name_itself_isnt_registered(camera):
    camera(_signed(1, device_id="-"))
    with pytest.raises(FlashError, match="didn't say which camera it is"):
        fl.provision_keys(board="OPENMV4", registrar=lambda *a: pytest.fail("registered"))


def test_without_registering_a_nameless_camera_is_fine(camera):
    camera(_signed(1, device_id="-"))
    assert len(fl.provision_keys(board="OPENMV4")) == 1


def test_dry_run_lists_the_registration():
    steps = fl.provision_keys(board="OPENMV4", dry_run=True, registrar=object())
    assert [s.label for s in steps] == ["keys", "register"]


def _project(tmp_path, account="acctA"):
    from openmv_ota.project.passphrase import dev_passphrase_path
    (tmp_path / "openmv-ota.toml").write_text(
        '[product]\nname = "p"\naccount_id = "%s"\n\n[ota]\nenabled = true\n'
        'server_url = "https://ota.test"\n\n[targets]\nboards = ["OPENMV4"]\n' % account,
        encoding="utf-8")
    p = dev_passphrase_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("dev-pass", encoding="utf-8")
    return tmp_path


def test_a_desk_that_isnt_set_up_fails_before_flashing(monkeypatch, tmp_path, capsys):
    root = _project(tmp_path)
    flashed = []
    monkeypatch.setattr(fl, "flash_factory", lambda *a, **k: flashed.append(1) or [])
    assert main(["flash", "factory", str(root), "-b", "OPENMV4", "--token", "t"]) != 0
    assert "no usable desk key" in capsys.readouterr().err and flashed == []
    assert main(["flash", "factory", str(tmp_path / "nowhere"), "-b", "OPENMV4"]) != 0
    assert "can't register cameras from this desk" in capsys.readouterr().err


def test_flash_factory_signs_and_uploads_a_record_the_server_can_verify(monkeypatch, tmp_path,
                                                                         capsys):
    from openmv_ota.ota import registration as reg
    from openmv_ota.project import desk_keys
    root = _project(tmp_path)
    desk = desk_keys.create(root, "bench-1", "dev-pass")
    monkeypatch.setattr(fl, "flash_factory", lambda *a, **k: [])
    monkeypatch.setattr(fl, "provision_keys", lambda **k: [
        fl.IdentityStep("keys made", ["port"]), k["registrar"]("OPENMV4", "01", PUB.hex(),
                                                                KX.hex())] and [])
    uploaded = []

    class FakeApi:
        def __init__(self, cfg):
            assert (cfg.server_url, cfg.token) == ("https://ota.test", "tok")

        def register_device(self, record, signature):
            uploaded.append((record, signature))
            return {"device_id": "OPENMV4:01", "account_id": "acctA", "fingerprint": "00" * 32,
                    "rekeyed": False}
    monkeypatch.setattr("openmv_ota.client.api.Api", FakeApi)
    assert main(["flash", "factory", str(root), "-b", "OPENMV4", "--token", "tok"]) == 0
    (record, signature), = uploaded
    got = reg.verify(record, signature, [desk.public()], account="acctA")
    assert (got.device_id, got.identity, got.exchange) == ("OPENMV4:01", PUB.hex(), KX.hex())


def test_a_registration_the_server_refuses_fails_the_flash(monkeypatch, tmp_path, capsys):
    from openmv_ota.client.errors import ClientError
    from openmv_ota.project import desk_keys
    root = _project(tmp_path)
    desk_keys.create(root, "bench-1", "dev-pass")
    monkeypatch.setattr(fl, "flash_factory", lambda *a, **k: [])
    monkeypatch.setattr(fl, "provision_keys",
                        lambda **k: k["registrar"]("OPENMV4", "01", PUB.hex(), KX.hex()))

    class Refusing:
        def __init__(self, cfg):
            pass

        def register_device(self, record, signature):
            raise ClientError("POST /api/v1/admin/registrations -> 404: not found", exit_code=1)
    monkeypatch.setattr("openmv_ota.client.api.Api", Refusing)
    assert main(["flash", "factory", str(root), "-b", "OPENMV4", "--token", "tok"]) != 0
    assert "-> 404" in capsys.readouterr().err

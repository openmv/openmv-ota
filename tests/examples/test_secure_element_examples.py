"""The secure-element demos in ``examples/secure_element``. The camera scripts run here against a
stand-in ``openmv_ota.se`` with host keys, and what they print is checked the way a user would
check it: the signature through ``verify_challenge.py``, the certificate as PEM."""

from __future__ import annotations

import datetime
import hashlib
import importlib.util
import subprocess
import sys
import types
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric import utils as ec_utils
from cryptography.x509.oid import NameOID

import openmv_ota

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "secure_element"
CAMERA_SCRIPTS = ["read_keys.py", "sign_challenge.py"]
VERIFY = EXAMPLES / "verify_challenge.py"
CONTEXT = b"omv-demo-challenge-v1\n"


def _load_verify():
    spec = importlib.util.spec_from_file_location("verify_challenge", VERIFY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


verify_challenge = _load_verify()


def _point(key):
    """The 65-byte public point of a private key, or of a certificate's key."""
    return key.public_key().public_bytes(serialization.Encoding.X962,
                                         serialization.PublicFormat.UncompressedPoint)


class SoftKeys:
    """The ``SecureElement`` interface on host keys, named like the frozen module that would hold
    it (``read_keys.py`` tells the kinds apart by that)."""

    __module__ = "openmv_se.soft"

    def __init__(self, cert=None):
        self.identity = ec.generate_private_key(ec.SECP256R1())
        self.exchange = ec.generate_private_key(ec.SECP256R1())
        self.cert = cert
        self.signed = []

    def public_key(self):
        return _point(self.identity)

    def ecdh_public_key(self):
        return _point(self.exchange)

    def certificate(self):
        return self.cert

    def sign(self, digest):
        assert len(digest) == 32
        self.signed.append(digest)
        return self.identity.sign(digest, ec.ECDSA(ec_utils.Prehashed(hashes.SHA256())))

    def random(self, n):
        assert 0 < n <= 32                     # the ATECC608's limit, the smallest of the kinds
        return bytes(range(0xA0, 0xA0 + n))


class Se050Keys(SoftKeys):
    __module__ = "openmv_se.se050"


class NotProvisioned(OSError):
    """Named like every driver's own."""


@pytest.fixture
def camera(monkeypatch, capsys):
    """``run(script, opened, edit=None)``: run a camera script with ``se.open()`` answering
    ``opened`` (keys, None, or an exception to raise), optionally editing one line of it first;
    what it printed."""
    se = types.ModuleType("openmv_ota.se")
    monkeypatch.setattr(openmv_ota, "se", se, raising=False)
    monkeypatch.setitem(sys.modules, "openmv_ota.se", se)

    def run(script, opened, edit=None):
        def open_():
            if isinstance(opened, BaseException):
                raise opened
            return opened

        se.open = open_
        path = EXAMPLES / script
        src = path.read_text(encoding="utf-8")
        if edit is not None:
            old, new = edit
            assert src.count(old) == 1
            src = src.replace(old, new)
        exec(compile(src, str(path), "exec"), {"__name__": "__main__"})
        return capsys.readouterr().out

    return run


@pytest.mark.parametrize("script", CAMERA_SCRIPTS)
def test_camera_scripts_compile(script):
    path = EXAMPLES / script
    compile(path.read_text(encoding="utf-8"), str(path), "exec")


# --- read_keys.py --------------------------------------------------------------------------

def test_read_keys_prints_both_keys_and_no_certificate_on_a_soft_board(camera):
    keys = SoftKeys()
    out = camera("read_keys.py", keys)
    assert "keys held by  the camera's own key area" in out
    assert "identity key  " + keys.public_key().hex() in out
    assert "exchange key  " + keys.ecdh_public_key().hex() in out
    assert "certificate   none" in out and "BEGIN" not in out


def test_read_keys_prints_the_se050_certificate_as_pem(camera):
    keys = Se050Keys()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "SE050 test")])
    when = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(keys.identity.public_key()).serial_number(1)
            .not_valid_before(when).not_valid_after(when + datetime.timedelta(days=1))
            .sign(keys.identity, hashes.SHA256()))
    keys.cert = cert.public_bytes(serialization.Encoding.DER)
    out = camera("read_keys.py", keys)
    assert "keys held by  NXP SE050 secure element" in out
    assert ("certificate   %d bytes, SHA-256 %s"
            % (len(keys.cert), hashlib.sha256(keys.cert).hexdigest())) in out
    pem = out[out.index("-----BEGIN"):]
    assert all(len(line) <= 64 for line in pem.splitlines())
    got = x509.load_pem_x509_certificate(pem.encode())
    assert got.public_bytes(serialization.Encoding.DER) == keys.cert
    assert _point(got) == keys.public_key()              # the identity key's certificate


# --- sign_challenge.py -> verify_challenge.py ----------------------------------------------

def _host_command(out):
    """The ``verify_challenge.py`` arguments the camera printed."""
    line = [ln for ln in out.splitlines() if ln.startswith("python verify_challenge.py ")]
    assert len(line) == 1
    return line[0].split()[2:]


def test_a_random_challenge_signed_on_the_camera_verifies_on_the_host(camera, capsys):
    keys = SoftKeys()
    out = camera("sign_challenge.py", keys)
    challenge = keys.random(32)
    assert keys.signed == [hashlib.sha256(CONTEXT + challenge).digest()]
    assert "challenge   " + challenge.hex() in out
    assert "public key  " + keys.public_key().hex() in out
    assert verify_challenge.main(_host_command(out)) == 0
    assert capsys.readouterr().out == "OK\n"


def test_the_challenge_set_at_the_top_is_the_one_signed(camera, capsys):
    keys = SoftKeys()
    out = camera("sign_challenge.py", keys, edit=('CHALLENGE = ""', 'CHALLENGE = "c0ffee00"'))
    assert keys.signed == [hashlib.sha256(CONTEXT + bytes.fromhex("c0ffee00")).digest()]
    args = _host_command(out)
    assert args[args.index("--challenge") + 1] == "c0ffee00"
    assert verify_challenge.main(args) == 0


# --- no keys -------------------------------------------------------------------------------

@pytest.mark.parametrize("script", CAMERA_SCRIPTS)
def test_a_camera_without_keys_is_told_how_to_make_them(camera, script):
    out = camera(script, NotProvisioned("se050: this camera has no exchange key; provision it"))
    assert "`openmv-ota flash factory` makes them" in out


@pytest.mark.parametrize("script", CAMERA_SCRIPTS)
def test_a_board_with_no_key_store_says_so(camera, script):
    assert "This board has no keys" in camera(script, None)


@pytest.mark.parametrize("script", CAMERA_SCRIPTS)
def test_damaged_keys_are_not_mistaken_for_none(camera, script):
    with pytest.raises(OSError, match="damaged"):
        camera(script, OSError("key store: the camera's newest key record is damaged"))


# --- verify_challenge.py -------------------------------------------------------------------

def _signed(challenge=b"\x01\x02\x03"):
    key = ec.generate_private_key(ec.SECP256R1())
    sig = key.sign(CONTEXT + challenge, ec.ECDSA(hashes.SHA256()))
    return key, challenge, sig


def _argv(pub, challenge, sig):
    return ["--public-key", pub.hex(), "--challenge", challenge.hex(), "--signature", sig.hex()]


def _flip(b, i=-1):
    b = bytearray(b)
    b[i] ^= 0x01
    return bytes(b)


def test_verify_accepts_a_good_signature(capsys):
    key, challenge, sig = _signed()
    assert verify_challenge.main(_argv(_point(key), challenge, sig)) == 0
    assert capsys.readouterr().out == "OK\n"


@pytest.mark.parametrize("tamper", ["challenge", "signature", "key", "not der"])
def test_verify_fails_anything_tampered(tamper, capsys):
    key, challenge, sig = _signed()
    pub = _point(key)
    if tamper == "challenge":
        challenge = _flip(challenge)
    elif tamper == "signature":
        sig = _flip(sig)                                 # the last byte of s
    elif tamper == "key":
        pub = _point(ec.generate_private_key(ec.SECP256R1()))
    else:
        sig = b"\x30\x00"
    assert verify_challenge.main(_argv(pub, challenge, sig)) == 1
    assert capsys.readouterr().out == "FAILED\n"


def test_verify_without_the_context_fails():
    """A signature over the bare challenge is not a demo signature."""
    key = ec.generate_private_key(ec.SECP256R1())
    sig = key.sign(b"\x01\x02\x03", ec.ECDSA(hashes.SHA256()))
    assert verify_challenge.main(_argv(_point(key), b"\x01\x02\x03", sig)) == 1


@pytest.mark.parametrize("argv", [
    ["--public-key", "zz", "--challenge", "00", "--signature", "00"],     # not hex
    ["--public-key", "04" + "00" * 64, "--challenge", "00", "--signature", "00"],  # not a point
    ["--challenge", "00", "--signature", "00"],                           # missing
])
def test_verify_rejects_bad_arguments(argv):
    with pytest.raises(SystemExit) as e:
        verify_challenge.main(argv)
    assert e.value.code == 2


def test_verify_exit_status_from_the_command_line():
    key, challenge, sig = _signed()
    for s, code, word in ((sig, 0, "OK"), (_flip(sig), 1, "FAILED")):
        r = subprocess.run([sys.executable, str(VERIFY), *_argv(_point(key), challenge, s)],
                           capture_output=True, text=True, timeout=60)
        assert (r.returncode, r.stdout.strip()) == (code, word)

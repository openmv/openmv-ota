"""Signed check-ins end to end on the host: the camera's signing (``trust.checkin_signature``,
``openmv_ota._signature_header``) against the server's check (``ota.checkin_sig``).

The two halves are written separately -- MicroPython on one side, ``cryptography`` on the other --
so the tests sign with the device code and verify with the server code, the way a real camera and
server meet.
"""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from openmv_ota.build.device import openmv_ota as rt
from openmv_ota.build.device import openmv_se
from openmv_ota.build.device.openmv_se import trust
from openmv_ota.ota import checkin_sig

BODY = b'{"device_id": "0036", "board": "OPENMV_N6"}'
TS = 1791720000


class _Keys:
    """A camera's keys: signs a digest the way every openmv_se driver does (DER ECDSA P-256)."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.signed = []

    def identity(self):
        return self.key.public_key().public_bytes(Encoding.X962,
                                                  PublicFormat.UncompressedPoint).hex()

    def sign(self, digest):
        self.signed.append(digest)
        return self.key.sign(digest, ec.ECDSA(Prehashed(hashes.SHA256())))


class _Rtc:
    def __init__(self, trusted=True, now=TS + 0.6):
        self._trusted, self._now = trusted, now

    def trusted(self):
        return self._trusted

    def now(self):
        return self._now


def test_both_sides_hash_the_same_thing():
    assert trust.CHECKIN_CONTEXT == checkin_sig.CONTEXT
    assert trust.checkin_digest(BODY, TS) == checkin_sig.digest(BODY, TS)


def test_a_camera_s_signature_verifies_at_the_server():
    keys = _Keys()
    header = trust.checkin_signature(BODY, TS, keys)
    assert header.startswith("v=1;ts=%d;sig=30" % TS)
    assert checkin_sig.verify(header, BODY, keys.identity(), TS + 10) == TS


def test_the_signature_covers_the_body_the_time_and_the_key():
    keys = _Keys()
    header = trust.checkin_signature(BODY, TS, keys)
    with pytest.raises(checkin_sig.CheckinSignatureError, match="bad signature"):
        checkin_sig.verify(header, BODY + b" ", keys.identity(), TS)
    moved = header.replace("ts=%d" % TS, "ts=%d" % (TS + 1))
    with pytest.raises(checkin_sig.CheckinSignatureError, match="bad signature"):
        checkin_sig.verify(moved, BODY, keys.identity(), TS)
    with pytest.raises(checkin_sig.CheckinSignatureError, match="bad signature"):
        checkin_sig.verify(header, BODY, _Keys().identity(), TS)       # another camera's key
    with pytest.raises(checkin_sig.CheckinSignatureError, match="bad signature"):
        checkin_sig.verify(header, BODY, "04abcd", TS)                 # not a point at all


@pytest.mark.parametrize("skew, ok", [(-300, True), (300, True), (-301, False), (301, False)])
def test_a_recorded_check_in_is_only_good_for_five_minutes(skew, ok):
    keys = _Keys()
    header = trust.checkin_signature(BODY, TS, keys)
    if ok:
        assert checkin_sig.verify(header, BODY, keys.identity(), TS + skew) == TS
    else:
        with pytest.raises(checkin_sig.CheckinSignatureError, match="stale"):
            checkin_sig.verify(header, BODY, keys.identity(), TS + skew)


@pytest.mark.parametrize("header, why", [
    ("v=2;ts=1;sig=00", "unknown version"),
    ("ts=1;sig=00", "unknown version"),
    ("v=1;ts=soon;sig=00", "malformed"),
    ("v=1;ts=1;sig=zz", "malformed"),
    ("v=1;ts=1", "malformed"),
    ("v=1;ts=1;sig=", "malformed"),
    ("v=1;v=1;ts=1;sig=00", "malformed"),
    ("v=1;junk", "malformed"),
    ("v=1;ts=1;sig=" + "00" * 300, "malformed"),
])
def test_a_malformed_header_says_so(header, why):
    with pytest.raises(checkin_sig.CheckinSignatureError, match=why):
        checkin_sig.parse(header)


def test_no_time_no_keys_or_a_failing_chip_means_no_signature(monkeypatch):
    assert trust.checkin_signature(BODY, None, _Keys()) is None    # no trusted clock

    class Broken:
        def sign(self, digest):
            raise OSError("chip not answering")

    assert trust.checkin_signature(BODY, TS, Broken()) is None
    monkeypatch.setattr(trust, "_keys", None)
    monkeypatch.setattr(openmv_se, "open", lambda: None)              # a board with no keys
    assert trust.checkin_signature(BODY, TS) is None


def test_the_keys_are_opened_once_and_kept(monkeypatch):
    keys, opened = _Keys(), []
    monkeypatch.setattr(trust, "_keys", None)
    monkeypatch.setattr(openmv_se, "open", lambda: opened.append(1) or keys)
    trust.checkin_signature(BODY, TS)
    trust.checkin_signature(BODY, TS + 1)
    assert opened == [1] and len(keys.signed) == 2


def test_the_header_line_is_sent_only_with_a_trusted_clock():
    keys = _Keys()

    class Se:
        @staticmethod
        def checkin_signature(payload, ts):
            return trust.checkin_signature(payload, ts, keys)

    line = rt._signature_header(BODY, rtc=_Rtc(), se=Se)
    assert line.startswith("X-OMV-Signature: v=1;ts=%d;" % TS) and line.endswith("\r\n")
    value = line[len("X-OMV-Signature: "):-2]
    assert checkin_sig.verify(value, BODY, keys.identity(), TS) == TS
    assert rt._signature_header(BODY, rtc=_Rtc(trusted=False), se=Se) == ""


def test_the_header_line_never_breaks_a_check_in():
    class NoKeys:
        @staticmethod
        def checkin_signature(payload, ts):
            return None

    class Raises:
        @staticmethod
        def checkin_signature(payload, ts):
            raise MemoryError

    class BadRtc(_Rtc):
        def trusted(self):
            raise OSError

    assert rt._signature_header(BODY, rtc=_Rtc(), se=NoKeys) == ""
    assert rt._signature_header(BODY, rtc=_Rtc(), se=Raises) == ""
    assert rt._signature_header(BODY, rtc=BadRtc(), se=NoKeys) == ""


def test_on_the_camera_it_uses_the_frozen_clock_and_keys(monkeypatch):
    import sys
    import types
    keys = _Keys()
    rtc = types.ModuleType("openmv_rtc")
    rtc.trusted, rtc.now = (lambda: True), (lambda: TS)
    se = types.ModuleType("openmv_se.trust")
    se.checkin_signature = lambda payload, ts: trust.checkin_signature(payload, ts, keys)
    monkeypatch.setitem(sys.modules, "openmv_rtc", rtc)
    monkeypatch.setitem(sys.modules, "openmv_se", types.SimpleNamespace(trust=se))
    value = rt._signature_header(BODY)[len("X-OMV-Signature: "):-2]
    assert checkin_sig.verify(value, BODY, keys.identity(), TS) == TS

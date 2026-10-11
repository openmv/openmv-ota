"""Registration records: a desk's signature that a camera's keys are an account's.

The record is what a broken-into server can't forge, so the tests lean on the ways a forged or
stale one would try to get through: a changed byte, another account, an unknown, revoked or
expired desk key, a back-dated or future-dated record.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from openmv_ota.ota import registration as reg
from openmv_ota.ota.algorithms import ES256, algorithm_for
from openmv_ota.ota.keys import generate_private_key, public_point_hex
from openmv_ota.ota.signer import LocalSigner

ALG = algorithm_for(ES256)
NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)


def _point():
    return public_point_hex(generate_private_key(ALG).public_key())


@pytest.fixture
def desk():
    key = generate_private_key(ALG)
    signer = LocalSigner(key, 1, ES256, ALG)
    public = reg.DeskKey(1, public_point_hex(key.public_key()), "2027-01-01T00:00:00Z")
    return signer, public


def _record(**kw):
    args = dict(account="acct_a", device_id="OPENMV_N6:0036", board="OPENMV_N6",
                identity=_point(), exchange=_point(), desk=1, issued=NOW)
    args.update(kw)
    return reg.build(**args)


def test_a_signed_record_verifies_and_says_what_it_registered(desk):
    signer, public = desk
    record = _record()
    got = reg.verify(record, reg.sign(record, signer), [public], account="acct_a")
    assert (got.account, got.device_id, got.board, got.desk) == \
        ("acct_a", "OPENMV_N6:0036", "OPENMV_N6", 1)
    assert got.issued == "2026-10-10T12:00:00Z" and len(bytes.fromhex(got.identity)) == 65
    reg.check_upload(got, [public], NOW + timedelta(minutes=1))


def test_the_record_is_its_bytes(desk):
    """Re-encoding the same data differently breaks the signature: records travel verbatim."""
    signer, public = desk
    record = _record()
    sig = reg.sign(record, signer)
    respaced = record.replace(b'","', b'", "')
    with pytest.raises(reg.RegistrationError, match="doesn't match"):
        reg.verify(respaced, sig, [public])


def test_a_changed_key_or_account_doesnt_verify(desk):
    signer, public = desk
    record = _record()
    sig = reg.sign(record, signer)
    other = _record(identity=_point())                    # same desk, another camera's key
    with pytest.raises(reg.RegistrationError, match="doesn't match"):
        reg.verify(other, sig, [public])
    with pytest.raises(reg.RegistrationError, match="another account"):
        reg.verify(record, sig, [public], account="acct_b")
    with pytest.raises(reg.RegistrationError, match="doesn't match"):
        reg.verify(record, "zz", [public])                # not even hex


def test_only_this_account_s_live_desk_keys_count(desk):
    signer, public = desk
    record = _record()
    sig = reg.sign(record, signer)
    with pytest.raises(reg.RegistrationError, match="isn't one of this account's"):
        reg.verify(record, sig, [])
    revoked = reg.DeskKey(1, public.public, public.expires, revoked=True)
    with pytest.raises(reg.RegistrationError, match="revoked"):
        reg.verify(record, sig, [revoked])
    lapsed = reg.DeskKey(1, public.public, "2026-10-01T00:00:00Z")
    with pytest.raises(reg.RegistrationError, match="had expired"):
        reg.verify(record, sig, [lapsed])


def test_a_record_stays_good_after_its_desk_key_expires(desk):
    """Expiry retires a desk, not the cameras it registered."""
    signer, public = desk
    record = _record()
    got = reg.verify(record, reg.sign(record, signer), [public])
    later = NOW + timedelta(days=400)                       # the key long expired
    assert got.device_id == "OPENMV_N6:0036"
    with pytest.raises(reg.RegistrationError, match="has expired"):
        reg.check_upload(got, [public], later)               # ...but can't upload any more


def test_uploads_must_be_fresh(desk):
    """A back-dated record (an old key's last trick) or a future-dated one is refused at upload."""
    signer, public = desk
    for issued, ok in ((NOW - timedelta(hours=2), False), (NOW + timedelta(minutes=10), False),
                       (NOW + timedelta(minutes=2), True), (NOW - timedelta(minutes=30), True)):
        record = _record(issued=issued)
        got = reg.verify(record, reg.sign(record, signer), [public])
        if ok:
            reg.check_upload(got, [public], NOW)
        else:
            with pytest.raises(reg.RegistrationError, match="issued just now"):
                reg.check_upload(got, [public], NOW)


@pytest.mark.parametrize("record, why", [
    (b"not json", "isn't JSON"),
    (b"[1, 2]", "fields aren't"),
    (b'{"v": 1}', "fields aren't"),
])
def test_malformed_records_are_refused(desk, record, why):
    with pytest.raises(reg.RegistrationError, match=why):
        reg.verify(record, "00", [desk[1]])


def test_wrong_types_versions_keys_and_times_are_refused(desk):
    import json
    signer, public = desk
    good = json.loads(_record())

    def bad(**kw):
        body = dict(good, **kw)
        record = json.dumps(body).encode()
        return record, reg.sign(record, signer)

    for kw, why in (({"v": 2}, "newer software"),
                    ({"desk": "1"}, "wrong types"),
                    ({"desk": True}, "wrong types"),
                    ({"board": 7}, "wrong types"),
                    ({"identity": "04" + "00" * 64}, "identity key isn't"),
                    ({"exchange": "05" + "11" * 64}, "exchange key isn't"),
                    ({"issued": "yesterday"}, "isn't a UTC timestamp"),
                    ({"issued": "2026-10-10T12:00:00"}, "isn't a UTC timestamp")):
        record, sig = bad(**kw)
        with pytest.raises(reg.RegistrationError, match=why):
            reg.verify(record, sig, [public])


def test_build_refuses_keys_that_arent_p256_points():
    with pytest.raises(reg.RegistrationError, match="identity key isn't"):
        _record(identity="04")
    with pytest.raises(reg.RegistrationError, match="exchange key isn't"):
        _record(exchange="nothex")

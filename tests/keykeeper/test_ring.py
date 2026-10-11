"""Keykeeper's keys: two generations live, each announced by the one before, so a publisher that
pinned any generation follows every rotation after it -- and nothing a server inserts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from openmv_ota.keykeeper import ring as kr
from openmv_ota.ota.keys import public_point_hex

NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
PASS = "keykeeper pass"


def _ring(tmp_path):
    return kr.Ring(tmp_path / "kk", PASS)


def test_init_makes_the_current_generation_and_the_next(tmp_path):
    r = _ring(tmp_path)
    gens = r.init(NOW, period_days=30)
    assert [g.key_id for g in gens] == [1, 2]
    assert gens[0].not_before == "2026-10-10T12:00:00Z"
    assert gens[1].not_before == "2026-11-09T12:00:00Z"
    assert kr.current(gens, NOW).key_id == 1
    assert kr.current(gens, NOW + timedelta(days=31)).key_id == 2
    assert b"ENCRYPTED" in (r.root / "private" / "kk-0001-seal.pem").read_bytes()
    assert r.seal_key(2).public_key().public_numbers() is not None
    with pytest.raises(kr.KeykeeperError, match="already holds"):
        r.init(NOW)
    with pytest.raises(kr.KeykeeperError, match="no keykeeper generation is in effect"):
        kr.current(gens, NOW - timedelta(days=1))


def test_a_pin_follows_the_chain_through_scheduled_rotations(tmp_path):
    r = _ring(tmp_path)
    pin = r.init(NOW, period_days=30)[0].pin()
    assert r.extend(NOW + timedelta(days=1), period_days=30) == []          # not due yet
    added = r.extend(NOW + timedelta(days=95), period_days=30)              # three periods on
    assert [g.key_id for g in added] == [3, 4, 5]
    gens = kr.verify_chain(r.chain(), pin)
    assert [g.key_id for g in gens] == [1, 2, 3, 4, 5]
    assert kr.current(gens, NOW + timedelta(days=95)).key_id == 4           # 5 is the next one
    later = kr.verify_chain(r.chain(), gens[2].pin())                       # pinned later on
    assert [g.key_id for g in later] == [3, 4, 5]


def test_an_emergency_rotation_is_in_effect_at_once(tmp_path):
    r = _ring(tmp_path)
    pin = r.init(NOW, period_days=30)[0].pin()
    gen = r.rotate_now(NOW + timedelta(hours=1), period_days=30)
    gens = kr.verify_chain(r.chain(), pin)
    assert gen.key_id == 3 and kr.current(gens, NOW + timedelta(hours=1)).key_id == 3
    assert gens[-1].not_before > gen.not_before                             # a next one after it


def test_a_generation_nobody_signed_is_refused(tmp_path):
    r = _ring(tmp_path)
    pin = r.init(NOW)[0].pin()
    chain = r.chain()
    rogue = ec.generate_private_key(ec.SECP256R1())
    record = json.dumps({"v": 1, "key_id": 3, "seal": public_point_hex(rogue.public_key()),
                         "sign": public_point_hex(rogue.public_key()),
                         "not_before": "2026-10-10T12:00:00Z"}, separators=(",", ":"))
    sig = rogue.sign(record.encode(), ec.ECDSA(hashes.SHA256())).hex()
    with pytest.raises(kr.KeykeeperError, match="isn't signed by 2"):
        kr.verify_chain(chain + [{"record": record, "signature": sig}], pin)
    skipped = record.replace('"key_id":3', '"key_id":4')
    with pytest.raises(kr.KeykeeperError, match="skips from 2 to 4"):
        kr.verify_chain(chain + [{"record": skipped, "signature": sig}], pin)


def test_the_pin_must_match_and_be_in_the_chain(tmp_path):
    r = _ring(tmp_path)
    gens = r.init(NOW)
    with pytest.raises(kr.KeykeeperError, match="isn't the one pinned"):
        kr.verify_chain(r.chain(), "1:" + gens[1].sign)
    with pytest.raises(kr.KeykeeperError, match="isn't in the chain"):
        kr.verify_chain(r.chain(), "9:" + gens[0].sign)
    with pytest.raises(kr.KeykeeperError, match="is '<key_id>:<sign key>'"):
        kr.verify_chain(r.chain(), "one:abc")
    assert r.genesis_pin() == gens[0].pin()


@pytest.mark.parametrize("mutate, why", [
    (lambda d: d.update(v=2), "version"),
    (lambda d: d.update(key_id=0), "malformed"),
    (lambda d: d.update(key_id=True), "malformed"),
    (lambda d: d.pop("seal"), "malformed"),
    (lambda d: d.update(seal="04ab"), "P-256"),
    (lambda d: d.update(not_before="soon"), "UTC timestamp"),
])
def test_malformed_records_are_refused(tmp_path, mutate, why):
    r = _ring(tmp_path)
    pin = r.init(NOW)[0].pin()
    chain = r.chain()
    d = json.loads(chain[0]["record"])
    mutate(d)
    chain[0] = dict(chain[0], record=json.dumps(d))
    with pytest.raises(kr.KeykeeperError, match=why):
        kr.verify_chain(chain, pin)


def test_a_missing_or_damaged_ring_says_so(tmp_path):
    r = _ring(tmp_path)
    with pytest.raises(kr.KeykeeperError, match="keykeeper init"):
        r.chain()
    r.root.mkdir()
    for text in ("{}", '{"chain": []}'):
        r.chain_path.write_text(text, encoding="utf-8")
        with pytest.raises(kr.KeykeeperError, match="damaged"):
            r.chain()
    with pytest.raises(kr.KeykeeperError, match="malformed"):
        kr.verify_chain([{"nope": 1}], "1:00")
    r.chain_path.unlink()
    r.init(NOW)
    with pytest.raises(kr.KeykeeperError, match="no seal key for keykeeper generation 7"):
        r.seal_key(7)

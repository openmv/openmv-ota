"""Desk keys: one per desk, encrypted at rest, expiring, revocable, and signing registrations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from openmv_ota.ota import registration as reg
from openmv_ota.ota.algorithms import ES256, algorithm_for
from openmv_ota.ota.keys import generate_private_key, public_point_hex
from openmv_ota.project import desk_keys as dk
from openmv_ota.project.errors import ProjectError

NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
PASS = "correct horse battery staple"
ALG = algorithm_for(ES256)


def _pass():
    return PASS, "file"


def test_a_project_starts_with_no_desk_keys(tmp_path):
    assert dk.read(tmp_path) == []
    with pytest.raises(ProjectError, match="no usable desk key"):
        dk.current(tmp_path, now=NOW)


def test_create_writes_the_public_half_and_encrypts_the_private(tmp_path):
    entry = dk.create(tmp_path, "bench-1", PASS, now=NOW)
    assert (entry.key_id, entry.label, entry.revoked) == (dk.id_for(entry.pubkey), "bench-1", False)
    assert entry.created == "2026-10-10T12:00:00Z" and entry.expires == "2027-01-08T12:00:00Z"
    pem = (tmp_path / "keys" / "private" / ("desk-%04x.pem" % entry.key_id)).read_bytes()
    assert b"ENCRYPTED" in pem
    assert dk.read(tmp_path) == [entry]
    second = dk.create(tmp_path, "factory-a", PASS, days=30, now=NOW)
    assert second.key_id != entry.key_id and second.expires == "2026-11-09T12:00:00Z"


def test_ids_come_from_the_key_so_two_projects_in_one_account_never_share_one(tmp_path):
    # Every project used to number its keys from 1, and the server holds an ACCOUNT's keys by id:
    # the second product's first key was refused. Ids from the key are 31 bits, never 0.
    a, b = (dk.create(tmp_path / p, "bench-1", PASS, now=NOW) for p in ("cam-a", "cam-b"))
    assert a.key_id != b.key_id and 0 < a.key_id < 2 ** 31
    assert dk.id_for("04" + "00" * 64) == dk.id_for("04" + "00" * 64)
    assert dk.id_for("00") == 1 or dk.id_for("00") > 0


def test_create_draws_again_on_a_clash_with_its_own_ids(tmp_path, monkeypatch):
    first = dk.create(tmp_path, "bench-1", PASS, now=NOW)
    real, calls = dk.id_for, []

    def clash_once(pubkey):
        calls.append(pubkey)
        return first.key_id if len(calls) == 1 else real(pubkey)
    monkeypatch.setattr(dk, "id_for", clash_once)
    second = dk.create(tmp_path, "factory-a", PASS, now=NOW)
    assert second.key_id != first.key_id and len(set(calls)) == 2


def test_an_id_of_zero_becomes_one(monkeypatch):
    class Digest:
        def digest(self):
            return bytes(32)
    monkeypatch.setattr(dk.hashlib, "sha256", lambda data: Digest())
    assert dk.id_for("04") == 1


def test_a_desk_key_signs_registrations_its_public_half_verifies(tmp_path):
    entry = dk.create(tmp_path, "bench-1", PASS, now=NOW)
    point = public_point_hex(generate_private_key(ALG).public_key())
    record = reg.build(account="acct_a", device_id="OPENMV4:01", board="OPENMV4",
                       identity=point, exchange=point, desk=entry.key_id, issued=NOW)
    sig = reg.sign(record, dk.signer(tmp_path, entry, passphrase_provider=_pass))
    assert reg.verify(record, sig, [entry.public()]).device_id == "OPENMV4:01"


def test_labels_are_one_live_key_per_desk(tmp_path):
    dk.create(tmp_path, "bench-1", PASS, now=NOW)
    with pytest.raises(ProjectError, match="already has a key"):
        dk.create(tmp_path, "bench-1", PASS, now=NOW)
    with pytest.raises(ProjectError, match="needs a label"):
        dk.create(tmp_path, "  ", PASS, now=NOW)
    dk.revoke(tmp_path, dk.read(tmp_path)[0].key_id)
    assert dk.create(tmp_path, "bench-1", PASS, now=NOW).label == "bench-1"   # a revoked one frees it


def test_current_is_the_newest_live_key_and_renew_brings_one_back(tmp_path):
    bench = dk.create(tmp_path, "bench-1", PASS, days=1, now=NOW)
    dk.create(tmp_path, "factory-a", PASS, now=NOW)
    later = NOW + timedelta(days=2)
    assert dk.current(tmp_path, now=later).label == "factory-a"
    with pytest.raises(ProjectError, match="for 'bench-1'"):
        dk.current(tmp_path, "bench-1", now=later)               # expired
    dk.renew(tmp_path, bench.key_id, now=later)
    assert dk.current(tmp_path, "bench-1", now=later).key_id == bench.key_id
    # newest is by when a key was made, not by its id (ids are no longer a counter): bench-1 and
    # factory-a were made together, and a tie goes to the key written last
    assert dk.current(tmp_path, now=later).label == "factory-a"
    newer = dk.create(tmp_path, "factory-b", PASS, now=later + timedelta(hours=1))
    assert dk.current(tmp_path, now=later + timedelta(hours=2)).key_id == newer.key_id
    with pytest.raises(ProjectError, match="no desk key 9"):
        dk.renew(tmp_path, 9)


def test_revoke_is_final(tmp_path):
    kid = dk.create(tmp_path, "bench-1", PASS, now=NOW).key_id
    assert dk.revoke(tmp_path, kid).revoked
    with pytest.raises(ProjectError, match="revoked"):
        dk.renew(tmp_path, kid, now=NOW)
    with pytest.raises(ProjectError, match="no usable"):
        dk.current(tmp_path, now=NOW)
    assert dk.read(tmp_path)[0].public().revoked


def test_a_backend_record_round_trips_and_a_damaged_file_says_so(tmp_path):
    entry = dk.create(tmp_path, "hsm-desk", PASS, now=NOW)
    entry.backend = {"backend": "pkcs11", "uri": "pkcs11:token=desk"}
    dk._write(tmp_path, [entry])
    assert dk.read(tmp_path)[0].backend == {"backend": "pkcs11", "uri": "pkcs11:token=desk"}
    dk.path_for(tmp_path).write_text("{not json", encoding="utf-8")
    with pytest.raises(ProjectError, match="damaged"):
        dk.read(tmp_path)


def test_by_default_it_is_now(tmp_path):
    entry = dk.create(tmp_path, "bench-1", PASS)
    assert dk.current(tmp_path).key_id == entry.key_id     # made now, good for 90 days

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
    assert (entry.key_id, entry.label, entry.revoked) == (1, "bench-1", False)
    assert entry.created == "2026-10-10T12:00:00Z" and entry.expires == "2027-01-08T12:00:00Z"
    pem = (tmp_path / "keys" / "private" / "desk-0001.pem").read_bytes()
    assert b"ENCRYPTED" in pem
    assert dk.read(tmp_path) == [entry]
    second = dk.create(tmp_path, "factory-a", PASS, days=30, now=NOW)
    assert second.key_id == 2 and second.expires == "2026-11-09T12:00:00Z"


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
    dk.revoke(tmp_path, 1)
    assert dk.create(tmp_path, "bench-1", PASS, now=NOW).key_id == 2   # a revoked one frees it


def test_current_is_the_newest_live_key_and_renew_brings_one_back(tmp_path):
    dk.create(tmp_path, "bench-1", PASS, days=1, now=NOW)
    dk.create(tmp_path, "factory-a", PASS, now=NOW)
    later = NOW + timedelta(days=2)
    assert dk.current(tmp_path, now=later).label == "factory-a"
    with pytest.raises(ProjectError, match="for 'bench-1'"):
        dk.current(tmp_path, "bench-1", now=later)               # expired
    dk.renew(tmp_path, 1, now=later)
    assert dk.current(tmp_path, "bench-1", now=later).key_id == 1
    with pytest.raises(ProjectError, match="no desk key 9"):
        dk.renew(tmp_path, 9)


def test_revoke_is_final(tmp_path):
    dk.create(tmp_path, "bench-1", PASS, now=NOW)
    assert dk.revoke(tmp_path, 1).revoked
    with pytest.raises(ProjectError, match="revoked"):
        dk.renew(tmp_path, 1, now=NOW)
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

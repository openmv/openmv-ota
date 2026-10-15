"""Keykeeper's backup: the ring packed exactly as it is on disk (keys still encrypted), the same
bytes every time, never a plaintext key -- and a restore that writes nothing until the chain
verifies and every key opens and is the key the chain says, and never over keys."""

from __future__ import annotations

import gzip
import io
import json
import tarfile
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from openmv_ota.keykeeper import ring as kr
from openmv_ota.ota.keys import private_key_pem

NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
PASS = "keykeeper pass"


@pytest.fixture
def ring(tmp_path):
    r = kr.Ring(tmp_path / "kk", PASS)
    r.init(NOW, period_days=30)
    r.rotate_now(NOW + timedelta(days=1), period_days=30)          # four generations
    return r


def _files(ring) -> dict[str, bytes]:
    files = {"chain.json": ring.chain_path.read_bytes()}
    for pem in (ring.root / "private").glob("*.pem"):
        files["private/" + pem.name] = pem.read_bytes()
    return files


def _plaintext(key=None) -> bytes:
    return (key or ec.generate_private_key(ec.SECP256R1())).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())


def test_a_backup_is_the_ring_as_it_is_on_disk_and_the_same_every_time(ring):
    archive = ring.backup()
    assert ring.backup() == archive
    assert archive[4:8] == bytes(4)                                  # no gzip timestamp
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        members = tar.getmembers()
        got = {m.name: tar.extractfile(m).read() for m in members}
    assert got == _files(ring)
    assert [m.name for m in members] == sorted(got)
    assert len(got) == 1 + 4 * 2
    assert {(m.mtime, m.mode, m.uid, m.uname) for m in members} == {(0, 0o600, 0, "")}
    assert all(b"BEGIN ENCRYPTED PRIVATE KEY" in got[n] for n in got if n != "chain.json")


def test_a_restore_puts_it_back_into_an_empty_directory_only(ring, tmp_path):
    archive = ring.backup()
    fresh = kr.Ring(tmp_path / "fresh", PASS)
    gens = fresh.restore(archive)
    assert gens == ring.generations() and _files(fresh) == _files(ring)
    assert fresh.seal_key(4).private_numbers() == ring.seal_key(4).private_numbers()
    assert (fresh.root / "private" / "kk-0001-seal.pem").stat().st_mode & 0o777 == 0o600
    assert not fresh.chain_path.with_suffix(".tmp").exists()
    with pytest.raises(kr.KeykeeperError, match="isn't empty"):
        fresh.restore(archive)                                       # never over keys
    with pytest.raises(kr.KeykeeperError, match="isn't empty"):
        ring.restore(archive)
    disk = kr.Ring(tmp_path / "disk", PASS)                          # a freshly formatted disk
    (disk.root / "lost+found").mkdir(parents=True)
    assert disk.restore(archive) == gens
    (tmp_path / "audit").mkdir()
    (tmp_path / "audit" / "audit.jsonl").write_text("{}\n")
    with pytest.raises(kr.KeykeeperError, match="isn't empty"):
        kr.Ring(tmp_path / "audit", PASS).restore(archive)


def test_a_plaintext_key_on_disk_is_never_backed_up(ring):
    seal = ring.root / "private" / "kk-0002-seal.pem"
    seal.write_bytes(_plaintext(ring.seal_key(2)))
    with pytest.raises(kr.KeykeeperError, match="kk-0002-seal.pem is not an encrypted key"):
        ring.backup()
    relabelled = _plaintext().replace(b"PRIVATE KEY", b"ENCRYPTED PRIVATE KEY")
    seal.write_bytes(relabelled)
    with pytest.raises(kr.KeykeeperError, match="not an encrypted key"):
        ring.backup()


def test_only_the_ring_s_files_are_backed_up(ring, tmp_path):
    (ring.root / "audit.jsonl").write_text("{}\n")                   # not keys: left out
    (ring.root / "private" / "notes.txt").write_text("x")
    assert "audit.jsonl" not in kr.read_backup(ring.backup())
    (ring.root / "private" / "spare.pem").write_bytes(
        private_key_pem(ec.generate_private_key(ec.SECP256R1()), PASS))
    with pytest.raises(kr.KeykeeperError, match="private/spare.pem doesn't belong"):
        ring.backup()
    with pytest.raises(kr.KeykeeperError, match="no keykeeper keys"):
        kr.Ring(tmp_path / "none", PASS).backup()


def test_what_counts_as_encrypted():
    encrypted = private_key_pem(ec.generate_private_key(ec.SECP256R1()), PASS)
    assert kr.pem_is_encrypted(encrypted)
    assert not kr.pem_is_encrypted(_plaintext())
    assert not kr.pem_is_encrypted(_plaintext().replace(b"PRIVATE KEY", b"ENCRYPTED PRIVATE KEY"))
    assert not kr.pem_is_encrypted(_plaintext().replace(b"PRIVATE KEY", b"EC PRIVATE KEY"))
    assert not kr.pem_is_encrypted(encrypted * 2)                   # one key per file
    assert not kr.pem_is_encrypted(b"")


def _tar(members) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            if data is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return gzip.compress(raw.getvalue())


def test_an_archive_that_doesn_t_open_is_refused(ring, tmp_path):
    fresh = kr.Ring(tmp_path / "fresh", PASS)
    archive = ring.backup()
    for bad in (b"", b"not a backup", archive[:len(archive) // 2], gzip.compress(b"x" * 600)):
        with pytest.raises(kr.KeykeeperError, match="doesn't open as a tar.gz"):
            fresh.restore(bad)
    with pytest.raises(kr.KeykeeperError, match="private isn't a plain file"):
        fresh.restore(_tar([("private", None)]))
    chain = ring.chain_path.read_bytes()
    with pytest.raises(kr.KeykeeperError, match="chain.json is in the backup twice"):
        fresh.restore(_tar([("chain.json", chain), ("chain.json", chain)]))
    assert not fresh.root.exists()                                   # nothing written


def _restore(files, tmp_path, passphrase=PASS):
    return kr.Ring(tmp_path / "fresh", passphrase).restore(kr._tar_gz(files))


def test_a_backup_that_wouldn_t_work_is_refused_before_anything_is_written(ring, tmp_path):
    good = _files(ring)

    def refused(files, why, passphrase=PASS):
        with pytest.raises(kr.KeykeeperError, match=why):
            _restore(files, tmp_path, passphrase)
        assert not (tmp_path / "fresh").exists()

    refused({k: v for k, v in good.items() if k != "chain.json"}, "has no chain.json")
    refused(dict(good, **{"../chain.json": good["chain.json"]}), "doesn't belong")
    refused(dict(good, **{"private/kk-0001-seal.pem": _plaintext()}), "not an encrypted key")
    refused(dict(good, **{"chain.json": b"{}"}), "the backup's chain.json is damaged")
    refused(dict(good, **{"chain.json": b'{"chain": [1]}'}), "chain is malformed")
    refused(dict(good, **{"chain.json": b'{"chain": [{}]}'}), "chain is malformed")
    chain = json.loads(good["chain.json"])["chain"]
    chain[2] = dict(chain[2], signature=chain[3]["signature"])
    refused(dict(good, **{"chain.json": json.dumps({"chain": chain}).encode()}),
            "generation 3 isn't signed by 2")
    refused(good, "kk-0001-seal.pem doesn't open with this passphrase", passphrase="wrong")
    refused({k: v for k, v in good.items() if k != "private/kk-0004-sign.pem"},
            "no sign key for generation 4")
    swapped = dict(good, **{"private/kk-0003-seal.pem": good["private/kk-0002-seal.pem"]})
    refused(swapped, "kk-0003-seal.pem isn't the seal key the chain announced for generation 3")


def test_a_key_minted_before_its_chain_entry_landed_is_restored_too(ring, tmp_path):
    orphan = private_key_pem(ec.generate_private_key(ec.SECP256R1()), PASS)
    files = dict(_files(ring), **{"private/kk-0005-seal.pem": orphan})
    gens = _restore(files, tmp_path)
    assert [g.key_id for g in gens] == [1, 2, 3, 4]
    assert (tmp_path / "fresh" / "private" / "kk-0005-seal.pem").read_bytes() == orphan

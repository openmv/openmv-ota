"""Tests for the per-board golden + release ledger (.openmv-ota/ledger.json)."""

from __future__ import annotations

from openmv_ota.project import ledger


def test_golden_record_and_read(tmp_path):
    assert ledger.golden_for(tmp_path, "OPENMV_N6") is None     # empty
    ledger.record_golden(tmp_path, "OPENMV_N6", version="1.0.0", payload_version=16777216,
                         sha256="ab" * 32, path="build/OPENMV_N6-factory-romfs.img")
    g = ledger.golden_for(tmp_path, "OPENMV_N6")
    assert g["version"] == "1.0.0" and g["path"].endswith("factory-romfs.img")
    # a second record overwrites (one golden per board)
    ledger.record_golden(tmp_path, "OPENMV_N6", version="1.0.1", payload_version=16777472,
                         sha256="cd" * 32, path="x")
    assert ledger.golden_for(tmp_path, "OPENMV_N6")["version"] == "1.0.1"


def test_release_record_and_last(tmp_path):
    assert ledger.last_release(tmp_path, "OPENMV_N6") is None
    ledger.record_release(tmp_path, "OPENMV_N6", version="1.1.0", payload_version=17825792,
                          sha256="ab" * 32, key_id=0x0100, when="t1")
    ledger.record_release(tmp_path, "OPENMV_N6", version="1.2.0", payload_version=18874368,
                          sha256="cd" * 32, key_id=0x0100, when="t2")
    last = ledger.last_release(tmp_path, "OPENMV_N6")
    assert last["version"] == "1.2.0" and last["ts"] == "t2"


def test_release_auto_timestamps(tmp_path):
    ledger.record_release(tmp_path, "B", version="1.0.0", payload_version=1, sha256="x",
                          key_id=1)                              # no `when`
    assert ledger.last_release(tmp_path, "B")["ts"].endswith("Z")


def test_corrupt_ledger_reads_as_empty(tmp_path):
    ledger.ledger_path(tmp_path).parent.mkdir(parents=True)
    ledger.ledger_path(tmp_path).write_text("{ this is not json")
    assert ledger.golden_for(tmp_path, "OPENMV_N6") is None     # no crash
    ledger.record_golden(tmp_path, "OPENMV_N6", version="1.0.0", payload_version=1,
                         sha256="x", path="y")                  # overwrites the junk
    assert ledger.golden_for(tmp_path, "OPENMV_N6")["version"] == "1.0.0"

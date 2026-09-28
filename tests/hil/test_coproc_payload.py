"""The coproc scenario's stress payload and its helper-core boot check (ci/hil/ota_cycle.py).

A one-chunk coprocessor image passes the MRAM write with or without the interrupt-masking fix, so
the harness pads the scaffolded app to COPROC_BLOB_BYTES and stamps a per-run nonce; after the
scenario scores, the helper core must read that nonce back off the partition it now boots from."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "ci", "hil"))

import ota_cycle  # noqa: E402


def _project(tmp_path, coproc=True):
    if coproc:
        (tmp_path / "app-coprocessor").mkdir()
    return str(tmp_path)


def test_payload_is_stress_sized_deterministic_and_nonced(tmp_path):
    proj = _project(tmp_path)
    nonce = ota_cycle.write_coproc_payload(proj, "OPENMV_AE3", nonce="n-1")
    assert nonce == "n-1"
    blob = (tmp_path / "app-coprocessor" / "hil_blob.bin").read_bytes()
    assert len(blob) == ota_cycle.COPROC_BLOB_BYTES
    assert len(blob) // 4096 >= 48, "must span enough 4 KB chunks to stress the MRAM write"
    assert blob[:32] != blob[32:64], "non-repeating, so a misplaced chunk fails readback"
    assert (tmp_path / "app-coprocessor" / "hil_nonce.txt").read_text() == "n-1"
    # deterministic across runs: only the nonce changes
    ota_cycle.write_coproc_payload(proj, "OPENMV_AE3", nonce="n-2")
    assert (tmp_path / "app-coprocessor" / "hil_blob.bin").read_bytes() == blob


def test_payload_default_nonce_is_unique_per_run(tmp_path):
    nonce = ota_cycle.write_coproc_payload(_project(tmp_path), "OPENMV_AE3")
    assert nonce.startswith("OPENMV_AE3-") and str(os.getpid()) in nonce


def test_payload_skips_boards_without_a_coprocessor(tmp_path):
    assert ota_cycle.write_coproc_payload(_project(tmp_path), "OPENMV_N6") is None
    assert not (tmp_path / "app-coprocessor" / "hil_blob.bin").exists()
    # a coprocessor board whose project has no app-coprocessor folder: also nothing to stress
    bare = tmp_path / "bare"
    bare.mkdir()
    assert ota_cycle.write_coproc_payload(str(bare), "OPENMV_AE3") is None


def _probe(monkeypatch, out, rc=0):
    seen = {}

    def fake_exec(code, timeout=60, check=True):
        seen["code"], seen["check"] = code, check
        return rc, out
    monkeypatch.setattr(ota_cycle, "device_exec", fake_exec)
    return seen


def test_he_boot_check_passes_on_this_runs_nonce(monkeypatch):
    size = ota_cycle.COPROC_BLOB_BYTES
    seen = _probe(monkeypatch, 'New service "vm"\n❯ HEROM n-7 %d\n❯ running tasks:0' % size)
    ok, why = ota_cycle.coproc_he_boot_check("OPENMV_AE3", "n-7")
    assert ok and "n-7" in why
    assert "RemoteProc(%d)" % 0x80320000 in seen["code"] and seen["check"] is False
    # the task is marshalled to the helper core, which rejects anything over 500 bytes of mpy;
    # keep the whole probe script small so the task body stays well under that
    assert len(ota_cycle._HE_PROBE) < 400


def test_he_boot_check_fails_on_a_stale_image(monkeypatch):
    _probe(monkeypatch, "❯ HEROM n-old %d" % ota_cycle.COPROC_BLOB_BYTES)
    ok, why = ota_cycle.coproc_he_boot_check("OPENMV_AE3", "n-new")
    assert not ok and "n-old" in why and "n-new" in why


def test_he_boot_check_fails_on_a_truncated_blob(monkeypatch):
    _probe(monkeypatch, "❯ HEROM n-7 4096")
    ok, why = ota_cycle.coproc_he_boot_check("OPENMV_AE3", "n-7")
    assert not ok and "4096" in why


def test_he_boot_check_fails_when_the_helper_core_is_silent(monkeypatch):
    calls, waits = [], []
    monkeypatch.setattr(ota_cycle, "device_exec", lambda *a, **k: (calls.append(1), (1, None))[1])
    monkeypatch.setattr(ota_cycle, "_ensure_cdc", lambda board, **k: waits.append(board))
    ok, why = ota_cycle.coproc_he_boot_check("OPENMV_AE3", "n-7")
    assert not ok and "never answered" in why and "rc=1" in why
    assert len(calls) == 3 and len(waits) == 3


def test_he_boot_check_rides_out_a_port_race(monkeypatch):
    """PR #91 gate: 'failed to access /dev/ttyACM0 (it may be in use)' right after the scored
    window. That is the port, not the helper core -- wait for the CDC and ask again."""
    answers = iter([(1, "mpremote: failed to access /dev/ttyACM0 (it may be in use by another "
                        "program)"),
                    (0, "\u276f HEROM n-7 %d" % ota_cycle.COPROC_BLOB_BYTES)])
    monkeypatch.setattr(ota_cycle, "device_exec", lambda *a, **k: next(answers))
    monkeypatch.setattr(ota_cycle, "_ensure_cdc", lambda board, **k: None)
    ok, why = ota_cycle.coproc_he_boot_check("OPENMV_AE3", "n-7")
    assert ok and "n-7" in why


def test_coproc_scenario_demands_the_boot_check_and_the_masked_write():
    sc = ota_cycle.SCENARIOS["coproc"]
    assert sc.get("he_boot") is True
    for mark in ("partition.write_masked", "partition.pad", "partition.complete",
                 "partition.file_stream", "sync.resource_written"):
        assert mark in sc["expect"]
        assert mark in ota_cycle.COVERAGE.values()
    skip = ota_cycle.SCENARIOS["coproc_skip"]
    assert "partition.write_masked" in skip["forbid"] and not skip.get("he_boot")


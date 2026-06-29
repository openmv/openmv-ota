"""Tests for the append-only project operations history (.openmv-ota/history.jsonl)."""

from __future__ import annotations

from openmv_ota.cli import main
from openmv_ota.project import history


def test_record_then_read_roundtrip(tmp_path):
    history.record(tmp_path, "build-romfs", when="2026-06-29T00:00:00Z",
                   boards=["OPENMV_N6"], ok=True)
    history.record(tmp_path, "keys-rotate", when="2026-06-29T00:01:00Z",
                   old=0x0100, new=0x0101)
    events = history.read(tmp_path)
    assert [e["action"] for e in events] == ["build-romfs", "keys-rotate"]   # append order
    assert events[0]["boards"] == ["OPENMV_N6"] and events[0]["ts"] == "2026-06-29T00:00:00Z"
    assert events[1]["old"] == 0x0100 and events[1]["new"] == 0x0101


def test_read_missing_is_empty(tmp_path):
    assert history.read(tmp_path) == []


def test_record_auto_timestamps(tmp_path):
    history.record(tmp_path, "x")                            # no `when` -> real UTC stamp
    ts = history.read(tmp_path)[0]["ts"]
    assert ts.endswith("Z") and ts[:4].isdigit()


def test_read_skips_corrupt_and_blank_lines(tmp_path):
    history.record(tmp_path, "good", when="t")
    with history.history_path(tmp_path).open("a", encoding="utf-8") as f:
        f.write("{not json\n")                              # a torn/partial line
        f.write("\n")                                       # a blank line
    history.record(tmp_path, "good2", when="t")
    assert [e["action"] for e in history.read(tmp_path)] == ["good", "good2"]


def test_record_is_best_effort(tmp_path):
    # a history write failure (here: the .openmv-ota dir is actually a file) must not raise
    (tmp_path / history.HISTORY_DIR).write_text("i am a file, not a dir")
    history.record(tmp_path, "x")                            # swallowed, no exception
    assert history.read(tmp_path) == []


# --- the `project history` viewer -------------------------------------------

def test_history_cli_empty(tmp_path, capsys):
    assert main(["project", "history", str(tmp_path)]) == 0
    assert "no recorded operations" in capsys.readouterr().out


def test_history_cli_lists_events_with_limit(tmp_path, capsys):
    for i in range(3):
        history.record(tmp_path, "build-romfs", when="t%d" % i, board="OPENMV_N6")
    assert main(["project", "history", str(tmp_path), "-n", "2"]) == 0
    out = capsys.readouterr().out
    assert out.count("build-romfs") == 2                    # only the last 2
    assert "board=OPENMV_N6" in out and "t2" in out and "t0" not in out

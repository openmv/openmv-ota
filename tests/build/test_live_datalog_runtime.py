"""Host tests for ``openmv_cloud.datalog`` -- structured telemetry to the datalake.

Pure logic: topic validation, the ``{sid, seq, data}`` record envelope, the
per-topic byte-record two-tier outbox (RAM ring, whole-backlog spill, take/
requeue), and ``post()`` sequencing + spool wiring. The flusher task, ``enable()``
and the HTTP ``_post`` wire device asyncio/sockets and are covered on hardware.
"""

from __future__ import annotations

import json

import pytest

from openmv_ota.build.device.openmv_cloud import datalog as dl


class _FakeDisk:
    def __init__(self):
        self.data = b""
    def append(self, d):
        self.data += d
    def size(self):
        return len(self.data)
    def read_all(self):
        return self.data
    def clear(self):
        self.data = b""
    def rewrite(self, d):
        self.data = d


def _records(disk):
    return [json.loads(x) for x in disk.data.split(b"\n") if x.strip()]


@pytest.fixture(autouse=True)
def _reset():
    dl._topics.clear()
    dl.clear_ingest()
    dl._spool_path = None
    dl._write_through = False
    yield
    dl._topics.clear()
    dl.clear_ingest()
    dl._spool_path = None
    dl._write_through = False


# --- topic validation -------------------------------------------------------

def test_valid_topics():
    assert dl._valid_topic("imu")
    assert dl._valid_topic("gps_1")
    assert dl._valid_topic("a-b-c")
    assert dl._valid_topic("9lives")


def test_invalid_topics():
    assert not dl._valid_topic("")
    assert not dl._valid_topic("console")          # reserved for logs
    assert not dl._valid_topic("Imu")              # uppercase
    assert not dl._valid_topic("with space")
    assert not dl._valid_topic("emoji☃")
    assert not dl._valid_topic("x" * 33)           # too long


# --- record envelope --------------------------------------------------------

def test_record_wraps_the_object_under_data():
    env = json.loads(dl._record("aa00", 7, {"ax": 1.2, "ay": 0.3}))
    assert env == {"sid": "aa00", "seq": 7, "data": {"ax": 1.2, "ay": 0.3}}


def test_record_payload_is_opaque():
    # any JSON value rides under data untouched -- list, nested, or a bare number
    assert json.loads(dl._record("s", 0, [1, 2, 3]))["data"] == [1, 2, 3]
    assert json.loads(dl._record("s", 1, 42))["data"] == 42


# --- the byte-record outbox -------------------------------------------------

def test_overflow_spills_the_whole_backlog_and_clears_ram():
    disk = _FakeDisk()
    ob = dl._ByteOutbox(cap_bytes=20, disk=disk)
    for i in range(4):
        ob.add(dl._record("aa00", i, i))           # each record well over 5 bytes
    assert ob.pending_bytes() == 0                 # RAM cleared on spill
    assert [r["seq"] for r in _records(disk)] == [0, 1, 2, 3]


def test_write_through_spills_every_record():
    disk = _FakeDisk()
    ob = dl._ByteOutbox(cap_bytes=1_000_000, disk=disk, write_through=True)
    ob.add(dl._record("aa00", 0, "one"))
    ob.add(dl._record("aa00", 1, "two"))
    assert ob.pending_bytes() == 0
    assert [r["seq"] for r in _records(disk)] == [0, 1]


def test_no_disk_is_ram_only_drop_oldest():
    ob = dl._ByteOutbox(cap_bytes=40, disk=None)
    for i in range(8):
        ob.add(dl._record("aa00", i, i))
    seqs = [json.loads(r)["seq"] for r in ob.take(10_000)]
    assert seqs[0] > 0 and seqs == sorted(seqs)    # oldest dropped, order kept


def test_take_packs_up_to_max_bytes_but_always_one():
    ob = dl._ByteOutbox(cap_bytes=1_000_000, disk=None)
    recs = [dl._record("aa00", i, i) for i in range(3)]
    for r in recs:
        ob.add(r)
    first = ob.take(1)                             # oversize budget still yields one
    assert first == [recs[0]]
    assert ob.pending_bytes() == len(recs[1]) + len(recs[2])


def test_requeue_over_cap_with_disk_does_not_drop():
    disk = _FakeDisk()
    ob = dl._ByteOutbox(cap_bytes=10, disk=disk)
    big = [b"a" * 8, b"b" * 8]
    ob.requeue(big)
    assert ob.pending_bytes() == 16                # disk present -> keep, no trim


def test_requeue_puts_records_back_at_the_front():
    ob = dl._ByteOutbox(cap_bytes=1_000_000, disk=None)
    ob.add(b"third")
    ob.requeue([b"first", b"second"])
    assert ob.take(1_000_000) == [b"first", b"second", b"third"]


def test_empty_outbox_take_is_none():
    assert dl._ByteOutbox().take(100) is None


# --- post() -----------------------------------------------------------------

def test_post_sequences_per_topic_and_wraps():
    assert dl.post("imu", {"ax": 1})
    assert dl.post("imu", {"ax": 2})
    assert dl.post("gps", {"lat": 3})
    imu = [json.loads(r) for r in dl._topics["imu"]["box"].take(10_000)]
    gps = [json.loads(r) for r in dl._topics["gps"]["box"].take(10_000)]
    assert [r["seq"] for r in imu] == [0, 1]        # seq is per topic
    assert [r["seq"] for r in gps] == [0]
    assert imu[0]["data"] == {"ax": 1} and gps[0]["data"] == {"lat": 3}
    assert imu[0]["sid"] == dl._sid


def test_post_rejects_a_bad_topic_and_queues_nothing():
    assert dl.post("BAD", {"x": 1}) is False
    assert "BAD" not in dl._topics


def test_post_wires_a_per_topic_spool_when_configured(monkeypatch):
    # with a spool path set, each topic's outbox gets its own disk file
    made = {}
    monkeypatch.setattr(dl, "_open_disk", lambda p: _FakeDisk())
    monkeypatch.setattr(dl, "_FileDisk", lambda path: made.setdefault(path, _FakeDisk()))
    dl._spool_path = "/sdcard"
    dl.post("imu", {"ax": 1})
    assert dl._topics["imu"]["box"]._disk is not None
    assert any("imu" in path for path in made)     # per-topic file name


def test_post_buffers_before_any_ingest_grant():
    # no ingest wired yet -- records still accrue (bounded), ready to upload later
    dl.post("temp", {"c": 21})
    assert dl._topics["temp"]["box"].pending_bytes() > 0


# --- ingest grant wiring ----------------------------------------------------

def test_set_and_clear_ingest():
    dl.set_ingest("https://data.example.com/", "tok")
    assert dl._ingest == ("https://data.example.com", "tok")
    dl.clear_ingest()
    assert dl._ingest is None


def test_set_ingest_with_missing_parts_disables():
    dl.set_ingest("https://x", None)
    assert dl._ingest is None
    dl.set_ingest(None, "tok")
    assert dl._ingest is None


def test_on_checkin_pulls_the_ingest_grant():
    dl._on_checkin({"ingest": {"url": "https://data.example.com", "token": "t9"}})
    assert dl._ingest == ("https://data.example.com", "t9")


def test_on_checkin_without_a_grant_is_a_noop():
    dl._on_checkin({"live": {"whatever": 1}})
    assert dl._ingest is None

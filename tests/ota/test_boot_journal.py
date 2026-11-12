"""The boot stub's journal: the 16-byte entry and the walk over a record block."""
import binascii
import struct

from openmv_ota.ota import boot_journal as bj
from openmv_ota.ota.install_record import REASONS


def _entry(seq=7, **kw):
    return bj.Entry(**dict(dict(slot=1, reason=2, cause_a=21, cause_b=0, seq=seq, events=0x11), **kw))


def test_an_entry_is_sixteen_bytes_and_reads_back():
    raw = bj.pack(_entry())
    assert len(raw) == bj.ENTRY_SIZE == 16
    assert raw[:6] == bytes([bj.TAG, 1, 2, 21, 0, 0])
    assert struct.unpack_from("<HI", raw, 6) == (7, 0x11)
    assert struct.unpack_from("<I", raw, 12)[0] == binascii.crc32(raw[:12]) & 0xFFFFFFFF
    assert bj.parse(raw) == [_entry()]
    assert bj.pack(_entry(seq=0x10007))[6:8] == b"\x07\x00"       # the sequence number wraps


def test_the_journal_is_the_second_half_of_the_record_block():
    assert bj.OFFSET == 2048 and bj.BLOCK == 4096
    block = bytearray(b"\xff" * bj.BLOCK)
    block[:4] = b"OMVI"                                           # a record: not the journal's
    for i in range(3):
        block[bj.OFFSET + 16 * i:bj.OFFSET + 16 * i + 16] = bj.pack(_entry(seq=i))
    assert [e.seq for e in bj.parse(block)] == [0, 1, 2]
    assert bj.parse(b"\xff" * bj.BLOCK) == []
    full = b"".join(bj.pack(_entry(seq=i)) for i in range(128))
    assert len(bj.parse(b"\xff" * bj.OFFSET + full)) == 128


def test_flash_that_takes_a_word_once_steps_by_its_word():
    half = bytearray(b"\xff" * 2048)
    for i in range(4):
        half[32 * i:32 * i + 16] = bj.pack(_entry(seq=i))
    assert [e.seq for e in bj.parse(half, stride=32)] == [0, 1, 2, 3]
    assert [e.seq for e in bj.parse(half, stride=16)] == [0]      # at 16 the gap reads as the end
    assert [e.seq for e in bj.parse(half)] == [0, 1, 2, 3]        # told nothing, it reads both
    assert [e.seq for e in bj.parse(half[:64] + half[:64])] == [0, 1, 0, 1]


def test_the_wire_form_is_read_leniently_and_named_in_words():
    wire = [[i, 0, 0, 0, 2, 0] for i in range(12)]
    assert [e.seq for e in bj.from_wire(wire)] == list(range(4, 12))      # the newest WIRE_MAX
    assert bj.from_wire(None) == [] and bj.from_wire([[1, 2], "x", [0, 0, 0, 0, 0, 1 << 32]]) == []
    e = bj.from_wire([[5, 1, 4, 26, 0, 1 << 4]])[0]
    assert e == bj.Entry(slot=1, reason=4, cause_a=26, cause_b=0, seq=5, events=16)
    assert bj.describe(e) == {"seq": 5, "slot": "B", "reason": "repaired", "slot_a": "blob",
                              "slot_b": "ok", "events": ["flash-reset-retry"]}
    assert bj.describe(bj.Entry(bj.SLOT_NONE, 5, 1, 1, 0))["slot"] is None
    assert bj.describe(bj.Entry(7, 0, 0, 0, 0))["slot"] == "unknown-7"


def test_what_a_server_has_not_seen_is_what_follows_the_last_it_saw():
    entries = [_entry(seq=s) for s in (3, 4, 5)]
    assert bj.after(entries, None) == entries
    assert [e.seq for e in bj.after(entries, 4)] == [5]
    assert bj.after(entries, 5) == []
    assert bj.after(entries, 9) == entries                        # no longer among them: all new
    assert bj.after([], 5) == []


def test_a_torn_entry_and_an_unknown_tag_are_skipped_not_fatal():
    torn = bytearray(bj.pack(_entry(seq=1)))
    torn[9] ^= 0x40
    newer = bytearray(bj.pack(_entry(seq=2)))
    newer[0] = 0x02
    data = bj.pack(_entry(seq=0)) + bytes(torn) + bytes(newer) + bj.pack(_entry(seq=3))
    assert [e.seq for e in bj.parse(data + b"\xff" * 16)] == [0, 3]


def test_numbers_a_reader_does_not_know_are_named_as_such():
    assert bj.name(bj.CAUSES, 21) == "trial-failed" and bj.name(bj.CAUSES, 200) == "unknown-200"
    assert bj.name(REASONS, 0) == "normal" and bj.name(REASONS, 99) == "unknown-99"
    assert bj.event_names(0) == []
    assert bj.event_names((1 << 4) | (1 << 0) | (1 << 31)) == [
        "area-encrypted", "flash-reset-retry", "unknown-31"]


def test_the_numbers_are_dense_and_named_once():
    for table in (bj.CAUSES, bj.EVENTS):
        assert sorted(table) == list(range(len(table)))
        assert len(set(table.values())) == len(table)
    assert bj.CAUSES[0] == "ok" and max(bj.EVENTS) < 32 and max(bj.CAUSES) < 255

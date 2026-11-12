"""The stub's journal (``common/journal.c``) against its Python twin, ``ota.boot_journal``.

Two things are held together here. The numbers: every cause, event bit and boot reason has
the same number and the same name in the stub's C and in the Python the server reads, and
every word the stub rejects a slot by has a number. And the behaviour: ``journal.c`` is
compiled for the host over a file that stands for the record's block, driven one boot at a
time, and what it leaves is read back by ``boot_journal.parse``.

Skipped without gcc.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from openmv_ota.build import stub as stub_mod
from openmv_ota.ota import boot_journal as bj
from openmv_ota.ota import install_record

COMMON = stub_mod.STUB_DIR / "common"


def _defines(text, prefix):
    return {m.group(1): int(m.group(2))
            for m in re.finditer(r"#define %s_(\w+)\s+\((\d+)\)" % prefix, text)}


def _word(name):
    return name.lower().replace("_", "-")


# --- the numbers -----------------------------------------------------------------------

def test_the_causes_have_the_same_numbers_and_names_in_c_and_python():
    causes = _defines((COMMON / "journal.h").read_text(), "CAUSE")
    count = causes.pop("COUNT")
    assert {n: _word(name) for name, n in causes.items()} == bj.CAUSES
    assert count == len(bj.CAUSES) == max(bj.CAUSES) + 1
    table = dict(re.findall(r'\[CAUSE_(\w+)\] = "([a-z-]+)"', (COMMON / "journal.c").read_text()))
    assert {causes[name]: word for name, word in table.items()} == bj.CAUSES


def test_the_event_bits_have_the_same_numbers_and_names_in_c_and_python():
    events = _defines((COMMON / "journal.h").read_text(), "EVENT")
    assert {n: _word(name) for name, n in events.items()} == bj.EVENTS


def test_the_boot_reasons_have_the_same_numbers_in_c_and_python():
    reasons = _defines((COMMON / "stub.h").read_text(), "REASON")
    assert {n: _word(name) for name, n in reasons.items()} == install_record.REASONS


def test_every_word_the_stub_rejects_a_slot_by_has_a_number():
    words = set()
    for name in ("verify.c", "main.c", "install.c"):
        text = (COMMON / name).read_text()
        words |= set(re.findall(r'reject\(s, "([a-z-]+)"\)', text))
        words |= set(re.findall(r'reject = "([a-z-]+)"', text))
    assert len(words) > 20
    assert words <= set(bj.CAUSES.values())


def test_the_layout_constants_agree():
    h = (COMMON / "journal.h").read_text()

    def const(name):
        return int(re.search(r"#define %s\s+\((\w+)\)" % name, h).group(1), 0)

    assert const("JOURNAL_OFFSET") == bj.OFFSET
    assert const("JOURNAL_ENTRY") == bj.ENTRY_SIZE
    assert const("JOURNAL_TAG") == bj.TAG
    assert const("JOURNAL_SLOT_NONE") == bj.SLOT_NONE
    assert const("JOURNAL_CARRY") == bj.CARRY


# --- the behaviour ---------------------------------------------------------------------

_CONFIG_H = """
#define STUB_RECORD_ADDR    0x1000
#define STUB_WRITE_UNIT     %d
#define STUB_FLASH_ORIGIN   0
#define STUB_FLASH_LENGTH   0
#define STUB_INSTALL_START  0
"""

_HARNESS_C = r"""
#include <stdio.h>
#include <stdlib.h>
#include "journal.h"
#include "port.h"

static uint8_t block[RECORD_BLOCK];
static int fail_write;

uint32_t crc32_of(const uint8_t *data, size_t len) {
    uint32_t crc = 0xFFFFFFFFU;
    for (size_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (int k = 0; k < 8; k++) crc = (crc >> 1) ^ (0xEDB88320U & (0U - (crc & 1)));
    }
    return ~crc;
}
void log_line(const char *s) { printf("%s\n", s); }
void log_kv(const char *k, uint32_t v) { printf("%s %u\n", k, v); }
int port_flash_prog_begin(void) { return 0; }
int port_flash_prog_end(void) { return 0; }
int port_flash_write(uint32_t addr, const uint8_t *buf, uint32_t size) {
    if (fail_write || size % STUB_WRITE_UNIT || addr % STUB_WRITE_UNIT) return -1;
    for (uint32_t i = 0; i < size; i++) block[addr - STUB_RECORD_ADDR + i] &= buf[i];   // flash: bits only clear
    return 0;
}

// One boot: <block file> <unread> <erase> <events> <causeA> <causeB> then commits:
//   <booted> <reason> <rejectA|-> <rejectB|->
int main(int argc, char **argv) {
    static slot_t slots[2];
    FILE *f = fopen(argv[1], "rb");
    if (fread(block, 1, sizeof(block), f) != sizeof(block)) return 1;
    fclose(f);
    if (!atoi(argv[2])) journal_load(block);
    journal_load(block);                                    // a second read changes nothing
    if (atoi(argv[3]) == 1) { memset(block, 0xFF, sizeof(block)); journal_erased(); }
    if (atoi(argv[3]) == 2) fail_write = 1;
    uint32_t events = (uint32_t) strtoul(argv[4], NULL, 0);
    for (uint32_t bit = 0; bit < 32; bit++) if (events & (1U << bit)) journal_event(bit);
    journal_cause(0, (uint8_t) atoi(argv[5]));
    journal_cause(1, (uint8_t) atoi(argv[6]));
    if (atoi(argv[5])) journal_cause(0, CAUSE_INSTALL);     // the first cause given stays
    journal_cause(2, CAUSE_INSTALL);                        // no such slot
    for (int a = 7; a + 3 < argc; a += 4) {
        for (int i = 0; i < 2; i++) {
            slots[i].index = (uint32_t) i;
            slots[i].ok = argv[a + 2 + i][0] == '-';
            slots[i].reject = slots[i].ok ? NULL : argv[a + 2 + i];
        }
        if (argv[a + 2][0] == '?') { slots[0].ok = false; slots[0].reject = NULL; }
        journal_commit(slots, (uint8_t) atoi(argv[a]), (uint8_t) atoi(argv[a + 1]));
    }
    f = fopen(argv[1], "wb");
    fwrite(block, 1, sizeof(block), f);
    fclose(f);
    return 0;
}
"""

pytest_gcc = pytest.mark.skipif(not shutil.which("gcc"), reason="needs gcc to compile the stub's journal")


class Camera:
    """A record block in a file and the journal's C, built for one write unit."""

    def __init__(self, tmp_path, unit):
        self.unit = unit
        self.stride = max(unit, bj.ENTRY_SIZE)
        self.block = tmp_path / "block.bin"
        self.block.write_bytes(b"\xff" * bj.BLOCK)
        (tmp_path / "stub_config.h").write_text(_CONFIG_H % unit)
        (tmp_path / "stub_keys.h").write_text("")
        (tmp_path / "harness.c").write_text(_HARNESS_C)
        self.exe = tmp_path / "journal"
        subprocess.run(["gcc", "-O1", "-Wall", "-Werror", "-I", str(tmp_path), "-I", str(COMMON),
                        str(tmp_path / "harness.c"), str(COMMON / "journal.c"), "-o", str(self.exe)],
                       check=True)

    def boot(self, *commits, unread=0, erase=0, events=0, cause_a=1, cause_b=0):
        """One boot. A commit is (booted, reason, reject A, reject B); ``cause_a`` is given
        to the C as the slot's explicit cause (the harness then offers "install", which must
        not replace it), 0 leaves the reject word to decide."""
        args = [str(self.exe), str(self.block), str(unread), str(erase), str(events),
                str(cause_a), str(cause_b)]
        for c in commits:
            args += [str(x) for x in c]
        return subprocess.run(args, check=True, capture_output=True, text=True).stdout

    def entries(self):
        return bj.parse(self.block.read_bytes(), self.stride)


@pytest.fixture(params=[16, 32])
def cam(request, tmp_path):
    if not shutil.which("gcc"):
        pytest.skip("needs gcc to compile the stub's journal")
    return Camera(tmp_path, request.param)


NORMAL = (0, 0, "-", "magic")       # booted A, normal; B is empty


def test_the_first_boot_writes_an_entry_and_the_same_boot_again_writes_none(cam):
    out = cam.boot(NORMAL)
    assert "journal entry 0" in out
    (e,) = cam.entries()
    # The harness gave slot A an explicit cause (read) although it booted: explicit wins.
    assert (e.slot, e.reason, e.cause_a, e.cause_b, e.seq) == (0, 0, 1, 2, 0)
    assert bj.event_names(e.events) == ["journal-restarted"]
    assert cam.boot(NORMAL) == ""
    assert len(cam.entries()) == 1


def test_a_boot_that_finds_something_else_or_does_something_writes_one(cam):
    cam.boot(NORMAL)
    cam.boot((1, 4, "-", "-"), cause_a=26)                  # repaired from B: A's blob
    cam.boot((1, 4, "-", "-"), cause_a=26)                  # the same again: nothing
    cam.boot((1, 4, "-", "-"), cause_a=26, events=1 << 4)   # the same, but the flash needed a retry
    got = [(e.slot, e.reason, bj.name(bj.CAUSES, e.cause_a), e.seq, bj.event_names(e.events))
           for e in cam.entries()]
    assert got == [(0, 0, "read", 0, ["journal-restarted"]),
                   (1, 4, "blob", 1, []),
                   (1, 4, "blob", 2, ["flash-reset-retry"])]


def test_a_reject_word_becomes_its_number_and_an_unknown_one_is_the_marks(cam):
    cam.boot((1, 0, "trial-failed", "-"), cause_a=0)
    cam.boot((1, 0, "no-such-word", "-"), cause_a=0)
    cam.boot((1, 0, "?", "-"), cause_a=0)                   # not ok, and no word at all
    assert [bj.name(bj.CAUSES, e.cause_a) for e in cam.entries()] == ["trial-failed", "status", "ok"]


def test_two_commits_in_one_boot_are_one_entry_unless_they_differ(cam):
    cam.boot(NORMAL, NORMAL, (bj.SLOT_NONE, 6, "-", "magic"))
    assert [(e.slot, e.reason, e.seq) for e in cam.entries()] == [(0, 0, 0), (bj.SLOT_NONE, 6, 1)]


def test_an_install_erases_the_block_and_the_last_entries_are_written_back(cam):
    for i in range(12):
        cam.boot((i % 2, 0, "-", "-"), cause_a=0)
    assert [e.seq for e in cam.entries()] == list(range(12))
    cam.boot((0, 1, "-", "-"), cause_a=0, erase=1)          # an install: a trial of A
    seqs = [e.seq for e in cam.entries()]
    assert seqs == list(range(12 - bj.CARRY, 13))
    assert (cam.entries()[-1].reason, cam.entries()[-1].events) == (1, 0)
    # The entries sit at the start of the journal, the record's half untouched.
    raw = cam.block.read_bytes()
    assert raw[:bj.OFFSET] == b"\xff" * bj.OFFSET
    assert raw[bj.OFFSET + (bj.CARRY + 1) * cam.stride:] == b"\xff" * (
        bj.BLOCK - bj.OFFSET - (bj.CARRY + 1) * cam.stride)


def test_an_install_on_an_ordinary_boot_is_still_an_entry(cam):
    cam.boot(NORMAL)
    cam.boot(NORMAL, erase=1)
    assert [e.seq for e in cam.entries()] == [0, 1]


def test_a_journal_lost_with_its_block_starts_again_and_says_so(cam):
    cam.boot(NORMAL)
    cam.boot((1, 0, "-", "-"))
    cam.block.write_bytes(b"\xff" * bj.BLOCK)               # the power went between erase and write
    cam.boot((1, 0, "-", "-"))
    (e,) = cam.entries()
    assert e.seq == 0 and bj.event_names(e.events) == ["journal-restarted"]


def test_a_full_journal_says_so_in_its_last_entry_and_then_stops(cam):
    total = (bj.BLOCK - bj.OFFSET) // cam.stride
    for i in range(total + 3):
        cam.boot((i % 2, 0, "-", "-"), cause_a=0)
    entries = cam.entries()
    assert len(entries) == total
    assert bj.event_names(entries[-1].events) == ["journal-full"]
    assert all(e.events & (1 << 9) == 0 for e in entries[1:-1])
    cam.boot((0, 2, "-", "-"), cause_a=0, erase=1)          # the next install makes room
    assert [e.seq for e in cam.entries()] == list(range(total - bj.CARRY, total + 1))


def test_a_torn_entry_and_a_newer_stubs_entry_are_stepped_over_not_written_over(cam):
    cam.boot(NORMAL)
    raw = bytearray(cam.block.read_bytes())
    at = bj.OFFSET + cam.stride
    raw[at:at + 16] = b"\x01" + b"\x00" * 15                # torn: this layout, a bad checksum
    raw[at + cam.stride:at + cam.stride + 16] = b"\x02" + b"\x55" * 15      # another layout
    raw[at + 2 * cam.stride + 3] = 0x00                     # torn before its tag was programmed
    cam.block.write_bytes(bytes(raw))
    cam.boot((1, 0, "-", "-"))
    assert [(e.slot, e.seq) for e in cam.entries()] == [(0, 0), (1, 1)]
    assert cam.block.read_bytes()[at:at + 16] == b"\x01" + b"\x00" * 15


def test_a_block_that_was_never_read_is_never_written(cam):
    assert cam.boot(NORMAL, unread=1) == "journal entry 0\n"    # the harness's second read did load it
    assert len(cam.entries()) == 1


def test_a_write_that_fails_is_logged_and_the_boot_goes_on(cam):
    out = cam.boot(NORMAL, erase=2)
    assert "journal write failed" in out
    assert cam.entries() == []

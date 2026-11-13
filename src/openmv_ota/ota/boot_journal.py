"""The boot stub's journal -- what the stub decided on the boots where it decided something.

The install record says what is installed. It is written once per install, so it cannot say
what happened on the boots since: a slot refused, the firmware area repaired from the other
slot, a flash chip that needed a second reset. Those lines reach the stub's UART and nothing
else, which in the field means nobody. The journal keeps them: one 16-byte entry per boot
that was not an ordinary one, appended by the stub to the second half of the install
record's 4 KiB block, read by ``boot.py`` and sent with the check-in.

Where: the record's block, from ``OFFSET`` (2048) to its end -- 128 entries. Flash that takes
a word only once (the H7's internal flash: 32 bytes) steps by its word, so an entry there is
followed by 16 bytes left erased and the journal holds 64. The block is erased by each
install; the stub reads the journal first and writes its last ``CARRY`` entries back after the
new record, so the boots that led to a repair or a revert outlive it.

An entry (little-endian)::

    tag(B) slot(B) reason(B) cause_a(B) cause_b(B) reserved(B)=0 seq(H) events(I) crc32(I)

The journal ends at the first entry that is erased, every byte 0xFF. ``tag``: ``TAG`` is
this layout; an entry with any other tag is one a newer stub wrote, or a write the power
cut short, and is skipped. ``slot``: the slot booted (0 = A,
1 = B), 0xFF when the stub booted none. ``reason``: why it booted, the install record's list
(``install_record.REASONS``). ``cause_a`` / ``cause_b``: why each slot was not the one booted,
0 when it verified. ``seq``: counts entries over the camera's life, wrapping; the server
keeps the last one it saw, which is the whole of the deduplication. ``events``: one bit per
thing that happened and is not a slot's fate. ``crc32`` is over the twelve bytes before it;
an entry that fails it is a torn write, and is skipped.

THE NUMBERS ARE FOR EVER. A reason, a cause or an event bit is given once and never moved
or reused; new ones take the next free number. Nothing here is keyed on the stub's version,
so a reader meets numbers it does not know and says so (``name`` returns ``unknown-<n>``)
rather than refusing the entry. The stub's C carries the same numbers (``journal.h``);
``tests/build/test_boot_journal_c.py`` holds the two together.
"""

from __future__ import annotations

import binascii
import struct
from dataclasses import dataclass

OFFSET = 2048                  # of the journal in the record's block
BLOCK = 4096                   # the record's block
ENTRY_STRUCT = "<BBBBBBHII"
ENTRY_SIZE = struct.calcsize(ENTRY_STRUCT)     # 16
TAG = 0x01
TAG_ERASED = 0xFF
SLOT_NONE = 0xFF
CARRY = 8                      # entries the stub writes back after an install erased the block

# Why a slot was not booted. 0 = it verified. Append only.
CAUSES = {
    0: "ok",
    1: "read",                 # the slot could not be read
    2: "magic",                # no trailer: an empty slot
    3: "crc",
    4: "framing",
    5: "version",
    6: "size",
    7: "board",
    8: "components",
    9: "comp-bounds",
    10: "comp-window",
    11: "comp-overlap",
    12: "cert",
    13: "cert-alg",
    14: "cert-sig",
    15: "root",
    16: "sig",
    17: "epoch",
    18: "min-stub",
    19: "rollback",
    20: "status",
    21: "trial-failed",
    22: "body",                # the image does not hash under the slot's cipher
    23: "convert",             # a slot in the clear could not be converted
    24: "erased",              # a slot in the clear, erased
    25: "attempt-mark",        # the trial's attempt could not be recorded
    26: "blob",                # a firmware blob does not hash to what the trailer names
    27: "install",             # a component did not install, or the record was not written
    28: "install-verify",      # the installed firmware does not hash through the memory map
}

# Things that happened on a boot and are not one slot's fate. Bit numbers. Append only.
EVENTS = {
    0: "area-encrypted",       # the firmware area was in the clear and was encrypted
    1: "slot-converted",       # a slot was in the clear and was converted
    2: "clear-slot-erased",    # a slot in the clear was erased
    3: "area-scrubbed",        # stale blocks past the firmware were erased
    4: "flash-reset-retry",    # the slots' flash chip answered only after a second reset
    5: "stray-fault",          # a fault was caught at the jump to the firmware (no stub sets it yet)
    6: "scrub-failed",
    7: "map-restore-failed",   # the flash's memory map could not be put back
    8: "journal-restarted",    # the sequence number was lost; this entry is new whatever it says
    9: "journal-full",         # the last entry that fitted: later boots were not recorded
    10: "unsigned-refused",    # secure boot: no verified slot, so the installed firmware was not booted
}


def name(table: dict, number: int) -> str:
    """The name a number has in ``CAUSES`` or ``install_record.REASONS``, or ``unknown-<n>``."""
    return table.get(number, "unknown-%d" % number)


def event_names(events: int) -> list:
    """The names of the bits set in ``events``, lowest first; an unnamed bit is ``unknown-<n>``."""
    return [name(EVENTS, bit) for bit in range(32) if events & (1 << bit)]


@dataclass
class Entry:
    slot: int                  # 0 = A, 1 = B, SLOT_NONE
    reason: int
    cause_a: int
    cause_b: int
    seq: int
    events: int = 0


def pack(e: Entry) -> bytes:
    body = struct.pack(ENTRY_STRUCT[:-1], TAG, e.slot, e.reason, e.cause_a, e.cause_b, 0,
                       e.seq & 0xFFFF, e.events & 0xFFFFFFFF)
    return body + struct.pack("<I", binascii.crc32(body) & 0xFFFFFFFF)


def parse(block, stride: int | None = None) -> list:
    """The entries in a record block (or in its journal half alone), oldest first.

    ``stride`` is the step between entries: 16, or the flash's word where it is larger. With
    none given the reader needs none: it steps by 16 and ends at the second erased step in a
    row, which is right for both (a 32-byte step leaves every other 16 bytes erased, and the
    stub never writes past an erased entry). An entry with an unknown tag or a bad checksum
    is skipped, and the entries after it still count."""
    data = bytes(block)
    if len(data) >= BLOCK:
        data = data[OFFSET:BLOCK]
    step = stride or ENTRY_SIZE
    entries, blanks = [], 0
    for at in range(0, len(data) - ENTRY_SIZE + 1, step):
        raw = data[at:at + ENTRY_SIZE]
        if data[at:at + step] == b"\xff" * len(data[at:at + step]):
            blanks += 1
            if stride or blanks == 2:
                break
            continue
        blanks = 0
        tag, slot, reason, cause_a, cause_b, _reserved, seq, events, crc = struct.unpack(
            ENTRY_STRUCT, raw)
        if tag != TAG or (binascii.crc32(raw[:-4]) & 0xFFFFFFFF) != crc:
            continue
        entries.append(Entry(slot, reason, cause_a, cause_b, seq, events))
    return entries


# --- on the wire ------------------------------------------------------------------------
# The check-in's ``stub.journal``: the newest entries, oldest first, each
# ``[seq, slot, reason, cause_a, cause_b, events]``.

WIRE_MAX = CARRY               # entries a check-in carries


def from_wire(journal) -> list:
    """The entries of a check-in's ``stub.journal``. Whatever is not a well-formed entry is
    left out: a camera is never refused over its journal. An entry is its first six numbers;
    anything a later stub adds after them is ignored here, not a reason to drop the entry."""
    out = []
    for item in (journal if isinstance(journal, list) else [])[-WIRE_MAX:]:
        item = item[:6] if isinstance(item, list) else item
        if (isinstance(item, list) and len(item) == 6
                and all(isinstance(x, int) and not isinstance(x, bool) for x in item)
                and 0 <= item[0] <= 0xFFFF and all(0 <= x <= 0xFF for x in item[1:5])
                and 0 <= item[5] <= 0xFFFFFFFF):
            out.append(Entry(item[1], item[2], item[3], item[4], item[0], item[5]))
    return out


def after(entries: list, last_seq) -> list:
    """The entries a server that last saw ``last_seq`` has not seen: those after it, or all
    of them when it saw none or the one it saw is no longer among them."""
    for i in range(len(entries) - 1, -1, -1):
        if entries[i].seq == last_seq:
            return entries[i + 1:]
    return list(entries)


def describe(e: Entry) -> dict:
    """An entry in words, for the audit trail."""
    from .install_record import REASONS
    slots = {0: "A", 1: "B", SLOT_NONE: None}
    return {"seq": e.seq, "slot": slots.get(e.slot, "unknown-%d" % e.slot),
            "reason": name(REASONS, e.reason),
            "slot_a": name(CAUSES, e.cause_a), "slot_b": name(CAUSES, e.cause_b),
            "events": event_names(e.events)}

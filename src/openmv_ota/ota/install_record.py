"""The boot stub's install record -- what it wrote to flash, and why it booted.

The stub keeps one record in the last 4 KiB of the firmware area it installs into, written
after every component has been decompressed and verified. On a normal boot the stub reads
it back: if the chosen slot's components match the record's hashes (and the firmware area
hashes to what was installed), it jumps without installing anything. A power cut
mid-install leaves no record, so the next boot installs again -- the record is the
"install finished" marker as much as the handoff.

It is also how ``boot.py`` learns which slot the stub chose and why, instead of a RAM
handoff: the record names the slot whose components are installed, and ``boot.py`` mounts
that slot's filesystem rather than choosing one itself. The record is derived from bytes
the stub has verified, so it carries no signature; a record that does not parse, or
names components that are not the chosen slot's, is simply ignored.

Layout (little-endian): a 32-byte fixed part, one 32-byte hash per component, a CRC::

    magic(4s)=OMVI record_version stub_version slot comp_count reason reserved(u64)
    sha256[comp_count] crc32

``reason``: 0 a normal boot, 1 a trial boot (the stub burned the slot's attempt), 2 the
stub reverted (the other slot failed its trial), 3 the stub reinstalled (the firmware area
was corrupt). ``slot``: 0 = A, 1 = B.
"""

from __future__ import annotations

import binascii
import struct
from dataclasses import dataclass, field

from .errors import OtaError
from .trailer import MAX_COMPONENTS

MAGIC = b"OMVI"
VERSION = 1
FIXED_STRUCT = "<4sIIIIIQ"
FIXED_SIZE = struct.calcsize(FIXED_STRUCT)     # 32
HASH_SIZE = 32
CRC_SIZE = 4
BLOCK = 4096                                   # the record's sector

REASON_NORMAL = 0
REASON_TRIAL = 1
REASON_REVERTED = 2
REASON_REINSTALLED = 3
REASONS = {REASON_NORMAL: "normal", REASON_TRIAL: "trial", REASON_REVERTED: "reverted",
           REASON_REINSTALLED: "reinstalled"}
SLOTS = ("A", "B")


@dataclass
class InstallRecord:
    stub_version: int
    slot: int                    # 0 = A, 1 = B
    reason: int
    hashes: list = field(default_factory=list)   # the installed components' blob hashes
    record_version: int = VERSION

    @property
    def slot_name(self) -> str:
        return SLOTS[self.slot]


def pack(r: InstallRecord) -> bytes:
    if r.slot not in (0, 1):
        raise OtaError("install record slot must be 0 or 1, got %d" % r.slot)
    if r.reason not in REASONS:
        raise OtaError("unknown install record reason %d" % r.reason)
    if len(r.hashes) > MAX_COMPONENTS:
        raise OtaError("%d component hashes, over the %d limit" % (len(r.hashes), MAX_COMPONENTS))
    for h in r.hashes:
        if len(h) != HASH_SIZE:
            raise OtaError("component hash must be %d bytes, got %d" % (HASH_SIZE, len(h)))
    body = struct.pack(FIXED_STRUCT, MAGIC, r.record_version, r.stub_version, r.slot,
                       len(r.hashes), r.reason, 0) + b"".join(bytes(h) for h in r.hashes)
    return body + struct.pack("<I", binascii.crc32(body) & 0xFFFFFFFF)


def parse(data) -> InstallRecord:
    """Decode a record block. Raises ``OtaError`` for anything that is not a complete, CRC-valid
    record of a known version -- which is what an erased sector, a torn write, or a record a
    newer stub wrote all look like to a reader that should then ignore it."""
    if len(data) < FIXED_SIZE:
        raise OtaError("install record too small")
    magic, version, stub_version, slot, comp_count, reason, _reserved = struct.unpack_from(
        FIXED_STRUCT, data, 0)
    if magic != MAGIC:
        raise OtaError("bad install record magic %r" % (magic,))
    if version != VERSION:
        raise OtaError("unsupported install record version %d" % version)
    if comp_count > MAX_COMPONENTS:
        raise OtaError("install record lists %d components, over the %d limit"
                       % (comp_count, MAX_COMPONENTS))
    end = FIXED_SIZE + comp_count * HASH_SIZE
    if end + CRC_SIZE > len(data):
        raise OtaError("install record truncated")
    if (binascii.crc32(bytes(data[:end])) & 0xFFFFFFFF) != struct.unpack_from("<I", data, end)[0]:
        raise OtaError("install record crc32 mismatch")
    if slot not in (0, 1):
        raise OtaError("install record names slot %d" % slot)
    if reason not in REASONS:
        raise OtaError("unknown install record reason %d" % reason)
    hashes = [bytes(data[FIXED_SIZE + i * HASH_SIZE:FIXED_SIZE + (i + 1) * HASH_SIZE])
              for i in range(comp_count)]
    return InstallRecord(stub_version, slot, reason, hashes, version)

"""Binary delta (patch) codec for OTA images -- a bsdiff-class delta against the golden.

A delta reconstructs a *new* image from the device's immutable golden (BACK slot). Like
bsdiff, each instruction has a **diff region** -- a run copied from the base with a
per-byte difference added back -- plus an **extra** run of literal new bytes, and a signed
**seek** of the base cursor. The diff stream means an *approximately* matching region
(a recompiled function, a table whose pointers all shifted) is encoded as one copy with a
mostly-zero difference, not re-inserted; the zeros vanish under gzip. The signed seek means
a region that slid forward or backward is still a cheap copy. This is the best-compressing
choice for the firmware/romfs case, where most of the image is unchanged model data and the
edits are small but may be scattered.

The reconstructed image is still verified by its sha256 (the manifest) and its signed
trailer (on boot), so a wrong/corrupt patch just yields a slot that fails verification ->
golden fallback. The patch is never trusted.

Wire format (the patch is gzipped for download; these are the decompressed bytes)::

    magic "OCDL" | target_size:uvarint | op*
    op := extra_len:uvarint  diff_len:uvarint  seek:svarint  extra[extra_len]  diff[diff_len]

Apply (``base`` = golden, ``old`` = base cursor, starts 0)::

    for each op:
        out += extra                       # literal new bytes
        old += seek                        # signed: align the base cursor
        out += (base[old:old+diff_len] + diff) mod 256   # copy-with-difference
        old += diff_len

The diff stream is one byte per copied byte, so the *uncompressed* patch is ~image-sized
(mostly zeros) -- it is always gzipped on the wire and **streamed** through the applier on
the device, never held whole in RAM.
"""

from __future__ import annotations

from .errors import OtaError

MAGIC = b"OCDL"
# Anchor length: the minimum exact run we index/match. _MAX_GAP is how many consecutive
# mismatched bytes a diff region tolerates before it's cut (so unrelated data becomes
# `extra`, not a diff of garbage). 32 / 64 suit firmware/romfs images.
_ANCHOR = 32
_MAX_GAP = 64


def _write_uvarint(out: bytearray, val: int) -> None:
    while True:
        b = val & 0x7F
        val >>= 7
        if val:
            out.append(b | 0x80)
        else:
            out.append(b)
            return


def _read_uvarint(buf, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7


def _zigzag(val: int) -> int:
    return (val << 1) if val >= 0 else (((-val) << 1) - 1)


def _unzigzag(zz: int) -> int:
    return (zz >> 1) if not (zz & 1) else -((zz + 1) >> 1)


def _write_svarint(out: bytearray, val: int) -> None:
    _write_uvarint(out, _zigzag(val))


def _read_svarint(buf, pos: int) -> tuple[int, int]:
    zz, pos = _read_uvarint(buf, pos)
    return _unzigzag(zz), pos


def make_delta(base: bytes, target: bytes) -> bytes:
    """Build a patch that reconstructs ``target`` from ``base``: index the base at anchor
    boundaries, roll over the target, and for each anchor match extend the copy region both
    ways -- backward exactly, forward through scattered mismatches (which become diff bytes)
    until ``_MAX_GAP`` consecutive misses -- then encode the leading literals as ``extra``
    and the aligned region as a diff."""
    index: dict[bytes, int] = {}
    for i in range(0, len(base) - _ANCHOR + 1, _ANCHOR):
        index.setdefault(base[i : i + _ANCHOR], i)

    out = bytearray(MAGIC)
    _write_uvarint(out, len(target))

    n = len(target)
    pos = lit_start = old_cursor = 0
    while pos + _ANCHOR <= n:
        off = index.get(target[pos : pos + _ANCHOR])
        if off is None:
            pos += 1
            continue
        sb, st = off, pos                               # backward exact extension
        while sb > 0 and st > lit_start and base[sb - 1] == target[st - 1]:
            sb -= 1
            st -= 1
        eb, et = off + _ANCHOR, pos + _ANCHOR           # forward approximate extension
        last_eb = eb
        gap = 0
        while eb < len(base) and et < n:
            if base[eb] == target[et]:
                eb += 1
                et += 1
                last_eb = eb
                gap = 0
            else:
                gap += 1
                if gap > _MAX_GAP:
                    break
                eb += 1
                et += 1
        diff_len = last_eb - sb                          # trim trailing mismatches
        extra = target[lit_start:st]
        _write_uvarint(out, len(extra))
        _write_uvarint(out, diff_len)
        _write_svarint(out, sb - old_cursor)
        out += extra
        for i in range(diff_len):
            out.append((target[st + i] - base[sb + i]) & 0xFF)
        old_cursor = sb + diff_len
        pos = st + diff_len
        lit_start = pos

    if lit_start < n:                                   # trailing literal (no diff region)
        _write_uvarint(out, n - lit_start)
        _write_uvarint(out, 0)
        _write_svarint(out, 0)
        out += target[lit_start:]
    return bytes(out)


def target_size(patch) -> int:
    """The reconstructed-image size declared in a patch header (for a build-time sanity
    check that a delta matches the image it's published alongside). Raises on bad magic."""
    if len(patch) < len(MAGIC) or bytes(patch[: len(MAGIC)]) != MAGIC:
        raise OtaError("not an OCDL delta")
    size, _pos = _read_uvarint(patch, len(MAGIC))
    return size


def apply_delta(base, patch) -> bytes:
    """Reconstruct the target from ``base`` + ``patch`` (host reference; the device mirrors
    this, streamed + ulab-accelerated). Raises ``OtaError`` on a malformed patch."""
    if len(patch) < len(MAGIC) or bytes(patch[: len(MAGIC)]) != MAGIC:
        raise OtaError("not an OCDL delta")
    target_sz, pos = _read_uvarint(patch, len(MAGIC))
    out = bytearray()
    old = 0
    end = len(patch)
    while len(out) < target_sz:
        if pos >= end:
            raise OtaError("delta truncated")
        extra_len, pos = _read_uvarint(patch, pos)
        diff_len, pos = _read_uvarint(patch, pos)
        seek, pos = _read_svarint(patch, pos)
        out += patch[pos : pos + extra_len]
        pos += extra_len
        old += seek
        if old < 0 or old + diff_len > len(base):
            raise OtaError("delta copy out of base bounds")
        src = base[old : old + diff_len]
        diff = patch[pos : pos + diff_len]
        if diff == bytes(diff_len):                     # exact region (the bulk) -> copy
            out += src
        else:
            out += bytes((src[i] + diff[i]) & 0xFF for i in range(diff_len))
        pos += diff_len
        old += diff_len
    if len(out) != target_sz:
        raise OtaError("delta produced %d bytes, header says %d" % (len(out), target_sz))
    return bytes(out)

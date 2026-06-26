"""Binary delta (patch) codec for OTA images -- a copy/insert delta against the golden.

A delta reconstructs a *new* image from the device's immutable golden (BACK slot) image:
the patch is a sequence of (insert literals, then copy a run from the base) instructions,
where the base cursor moves by a **signed seek** before each copy -- so a region that
slid forward or backward in the address space is still a cheap copy, not a re-insert.

Unlike bsdiff there is no byte-difference stream: a copy is an *exact* run from the base.
That makes the on-device applier trivial -- ``bytearray`` slices from the XIP'd BACK slot,
no per-byte arithmetic (so no ulab/C needed, and it runs on every board). It compresses
the dominant OTA case (model blobs unchanged, a few small edits) about as well as bsdiff;
it gives up bsdiff's edge only on *scattered* small edits across a large region.

The patch is pure transport: the reconstructed image is still verified by its sha256 (the
manifest) and its signed trailer (on boot), so a wrong/corrupt patch just yields a slot
that fails verification -> golden fallback. The patch is never trusted.

Wire format (the patch is gzipped for download; these are the decompressed bytes)::

    magic "OCDL" | target_size:uvarint | op*
    op := insert_len:uvarint  copy_len:uvarint  seek:svarint  insert_bytes[insert_len]

Apply (``base`` = golden bytes, ``old`` = base cursor, starts 0)::

    for each op:  out += insert_bytes;  old += seek;  out += base[old:old+copy_len];  old += copy_len
"""

from __future__ import annotations

from .errors import OtaError

MAGIC = b"OCDL"
# Anchor length: the minimum run we index/match. Smaller finds more (smaller) matches at
# the cost of a bigger index; 32 is a good balance for firmware/romfs images.
_ANCHOR = 32


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
    """Build a patch that reconstructs ``target`` from ``base`` (rsync-style: index the
    base at anchor-stride boundaries, roll over the target, extend each match both ways)."""
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
        start_b, start_t = off, pos
        while start_b > 0 and start_t > lit_start and base[start_b - 1] == target[start_t - 1]:
            start_b -= 1
            start_t -= 1
        end_b, end_t = off + _ANCHOR, pos + _ANCHOR
        while end_b < len(base) and end_t < n and base[end_b] == target[end_t]:
            end_b += 1
            end_t += 1
        literal = target[lit_start:start_t]
        _write_uvarint(out, len(literal))
        _write_uvarint(out, end_b - start_b)
        _write_svarint(out, start_b - old_cursor)
        out += literal
        old_cursor = end_b
        pos = end_t
        lit_start = end_t

    if lit_start < n:                                   # trailing literal (copy_len 0)
        _write_uvarint(out, n - lit_start)
        _write_uvarint(out, 0)
        _write_svarint(out, 0)
        out += target[lit_start:]
    return bytes(out)


def apply_delta(base, patch) -> bytes:
    """Reconstruct the target from ``base`` + ``patch`` (host reference; the device mirrors
    this, streaming the output). Raises ``OtaError`` on a malformed patch or size mismatch."""
    if len(patch) < len(MAGIC) or bytes(patch[: len(MAGIC)]) != MAGIC:
        raise OtaError("not an OCDL delta")
    target_size, pos = _read_uvarint(patch, len(MAGIC))
    out = bytearray()
    old = 0
    end = len(patch)
    while pos < end:
        insert_len, pos = _read_uvarint(patch, pos)
        copy_len, pos = _read_uvarint(patch, pos)
        seek, pos = _read_svarint(patch, pos)
        out += patch[pos : pos + insert_len]
        pos += insert_len
        old += seek
        if old < 0 or old + copy_len > len(base):
            raise OtaError("delta copy out of base bounds")
        out += base[old : old + copy_len]
        old += copy_len
    if len(out) != target_size:
        raise OtaError("delta produced %d bytes, header says %d" % (len(out), target_size))
    return bytes(out)

"""Locate the signed trailer(s) inside a partition / factory ROMFS image.

``build romfs`` emits a single trailer (in a ``.zip`` bundle, or as a loose
``trailer.bin``); ``build factory-romfs`` emits a full dual-slot partition image --
FRONT then BACK, each ``body || 0xFF pad || status || trailer`` with the trailer in
the last erase block of its slot. These helpers find each slot's trailer by
scanning block-aligned offsets for the magic and CRC-validating it, so ``inspect``
and ``verify`` can work on a factory image with no project context.
"""

from __future__ import annotations

from .errors import OtaError
from .trailer import HEADER_SIZE, MAGIC_ROMFS_APP, Trailer, parse_trailer

# Trailers sit at an erase-block boundary, and every board's block is a multiple of
# 4 KiB, so a 4 KiB stride lands on each one regardless of the actual block size.
SCAN_STEP = 4096


def find_trailers(image: bytes, step: int = SCAN_STEP) -> list[tuple[int, Trailer]]:
    """Every CRC-valid trailer in ``image`` as ``(offset, Trailer)``, in image order.

    The CRC check inside :func:`parse_trailer` means a stray ``OMVR`` in body/meta
    data can't masquerade as a trailer -- only genuine ones are returned."""
    out: list[tuple[int, Trailer]] = []
    off, n = 0, len(image)
    while off + HEADER_SIZE <= n:
        if image[off:off + 4] == MAGIC_ROMFS_APP:
            try:
                out.append((off, parse_trailer(bytes(image[off:]))))
            except OtaError:
                pass
        off += step
    return out


def slot_labels(n: int) -> list[str]:
    """Human labels for ``n`` trailers found in an image: ``FRONT``/``BACK`` for a
    two-slot factory image, ``image`` for each trailer otherwise."""
    return ["FRONT", "BACK"] if n == 2 else ["image"] * n


def slots(image: bytes, step: int = SCAN_STEP) -> list[tuple[str, bytes, bytes]]:
    """Each slot as ``(label, body, trailer_bytes)``. Empty when the image carries
    no trailer (a plain, unsigned romfs).

    A slot's body is the ``body_size`` bytes at its start; the next slot begins
    after this slot's trailer block (``len(image) - last_trailer_offset``)."""
    found = find_trailers(image, step)
    if not found:
        return []
    block = len(image) - found[-1][0]   # the last trailer fills the last erase block
    out, start = [], 0
    for (off, t), label in zip(found, slot_labels(len(found))):
        out.append((label, bytes(image[start:start + t.body_size]), bytes(image[off:])))
        start = off + block
    return out

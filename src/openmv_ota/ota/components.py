"""Firmware components -- the blobs an OTA image carries for the boot stub.

A v4 slot is ``filesystem image | blob | blob | ... | 0xFF pad | status | trailer``. Each blob
is a **component**: bytes the boot stub decompresses to a flash address before the firmware
runs (the firmware itself, a second core's firmware, a radio blob). The trailer lists them
(``openmv_ota.ota.trailer.Component``: destination, offset in the slot, compressed size,
decompressed size, SHA-256 of the compressed bytes). This module is the codec for the blobs
and the layout rule for where they sit:

- **Raw LZMA1, 256 KiB dictionary, lc=3 lp=0 pb=2.** Fixed for the life of the format: the
  stub's decoder (``LzmaDec.c`` from the LZMA SDK) is built for exactly these properties and
  sizes its dictionary buffer from them. A different codec would be a trailer version bump.
- **Compressed once per firmware build, bit-identical across releases.** ``lzma`` with fixed
  filters is deterministic, so a release that ships the same firmware as the last one
  carries the same bytes, and the delta between them encodes the blob as one copy
  (measured: ~1 KB for an unchanged firmware, the full blob for a changed one).
- **Blobs start on a 16-byte boundary** after the filesystem image. Nothing requires it --
  the stub reads the slot memory-mapped -- but it is the flash write unit everywhere, which
  keeps every blob's first byte on a unit boundary for the tools that write slots in pieces.
"""

from __future__ import annotations

import hashlib
import lzma

from .errors import OtaError
from .trailer import Component

LZMA_DICT_SIZE = 256 * 1024
LZMA_FILTERS = [{"id": lzma.FILTER_LZMA1, "preset": 9 | lzma.PRESET_EXTREME,
                 "dict_size": LZMA_DICT_SIZE, "lc": 3, "lp": 0, "pb": 2}]
BLOB_ALIGN = 16


def compress(data: bytes) -> bytes:
    """``data`` as the raw LZMA1 stream the boot stub decodes."""
    return lzma.compress(bytes(data), format=lzma.FORMAT_RAW, filters=LZMA_FILTERS)


def decompress(blob: bytes, size: int) -> bytes:
    """The ``size`` bytes a blob decompresses to. Raises ``OtaError`` for a corrupt stream or
    one whose length is not ``size`` -- which is what the stub would find too."""
    try:
        out = lzma.decompress(bytes(blob), format=lzma.FORMAT_RAW, filters=LZMA_FILTERS)
    except lzma.LZMAError as e:
        raise OtaError("component does not decompress: %s" % e) from None
    if len(out) != size:
        raise OtaError("component decompresses to %d bytes, trailer says %d" % (len(out), size))
    return out


def append(body: bytes, blobs) -> tuple[bytes, list[Component]]:
    """Lay ``blobs`` (``[(dest, installed_size, compressed_bytes), ...]``) out after ``body``.
    Returns the image (``body`` plus aligned blobs) and the trailer's component entries."""
    image = bytearray(body)
    comps = []
    for dest, installed_size, blob in blobs:
        if len(image) % BLOB_ALIGN:
            image += b"\xff" * (BLOB_ALIGN - len(image) % BLOB_ALIGN)
        comps.append(Component(dest=dest, offset=len(image), size=len(blob),
                               installed_size=installed_size,
                               sha256=hashlib.sha256(blob).digest()))
        image += blob
    return bytes(image), comps


def check(image: bytes, components) -> str | None:
    """Why ``image`` does not carry ``components`` as its trailer lists them (a reason), or
    None when every blob is where the trailer says with the hash it says."""
    for c in components:
        if c.offset + c.size > len(image):
            return "component at +%d (%d bytes) runs past the %d-byte image" % (
                c.offset, c.size, len(image))
        if hashlib.sha256(image[c.offset:c.offset + c.size]).digest() != c.sha256:
            return "component at +%d hash mismatch" % c.offset
    return None

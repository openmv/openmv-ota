"""OpenMV ROMFS container format — writer and reader.

A faithful, dependency-free Python port of the OpenMV IDE's reference
implementation (``qt-creator/src/plugins/openmv/tools/romfs.cpp``,
``VfsRomWriter`` / ``VfsRomReader``). This is the on-flash format the firmware's
``vfs.VfsRom`` mounts; it matches MicroPython's mpremote romfs writer, plus the
per-extension *alignment rules* OpenMV needs so memory-mapped assets (e.g. NPU
model blobs) land on the required byte boundary.

Format summary (little detail matters, so it is reproduced exactly):

* Unsigned integers are base-128, big-endian, with the high bit set on every
  byte except the last (``encode_uint``).
* A *record* is ``kind | padding | size | payload``. ``kind`` and ``size`` are
  ``encode_uint`` values; ``payload`` is ``size`` bytes. Alignment padding is a
  run of ``0x80`` bytes inserted between ``kind`` and ``size`` — each ``0x80`` is
  a continuation byte worth zero, so the decoder absorbs them as leading-zero
  bits of ``size`` and the payload start lands on the requested boundary.
* The whole image is one ``ROMFS`` header record (kind = the 3 magic bytes
  ``0xD2 0xCD 0x31``) whose payload, aligned to 16, is the root directory's
  concatenated child records.
* File data is a ``FILE`` record (aligned to 8) wrapping a name record plus a
  ``DATA_VERBATIM`` record; the alignment rule for the file's extension is
  applied to the ``DATA_VERBATIM`` payload so the bytes themselves are aligned.

The writer tracks absolute byte offsets through nested directories so alignment
is computed against the payload's real position in the final image.
"""

from __future__ import annotations

import os
from typing import Iterable, Mapping

# --- Format constants (from romfs.cpp) --------------------------------------

ROMFS_SIZE_MIN = 4

ROMFS_HEADER_BYTE0 = 0x80 | ord("R")  # 0xD2
ROMFS_HEADER_BYTE1 = 0x80 | ord("M")  # 0xCD
ROMFS_HEADER_BYTE2 = 0x00 | ord("1")  # 0x31
ROMFS_HEADER_MAGIC = bytes((ROMFS_HEADER_BYTE0, ROMFS_HEADER_BYTE1, ROMFS_HEADER_BYTE2))

# The header record's "kind" is the three magic bytes read as one base-128 uint.
ROMFS_HEADER_KIND = (
    ((ROMFS_HEADER_BYTE0 & 0x7F) << 14)
    | ((ROMFS_HEADER_BYTE1 & 0x7F) << 7)
    | (ROMFS_HEADER_BYTE2 & 0x7F)
)

ROMFS_RECORD_KIND_UNUSED = 0
ROMFS_RECORD_KIND_PADDING = 1
ROMFS_RECORD_KIND_DATA_VERBATIM = 2
ROMFS_RECORD_KIND_DATA_POINTER = 3
ROMFS_RECORD_KIND_DIRECTORY = 4
ROMFS_RECORD_KIND_FILE = 5

ROMFS_MIN_ALIGNMENT = 4
ROMFS_FILEREC_ALIGNMENT = 8
ROMFS_HEADER_ALIGNMENT = 16

_PADDING_BYTE = 0x80


# --- Helpers ----------------------------------------------------------------

def to_ascii(name: str) -> bytes:
    """Replicate the IDE's ``toAscii``: Latin-1, non-printables -> ``?``."""
    out = bytearray(name.encode("latin-1", "replace"))
    for i, ch in enumerate(out):
        if ch < 0x20 or ch > 0x7E:
            out[i] = ord("?")
    return bytes(out)


def encode_uint(value: int) -> bytes:
    """Base-128, big-endian, continuation high bit on all but the last byte."""
    if value < 0:
        raise ValueError("encode_uint requires a non-negative value")
    encoded = bytearray((value & 0x7F,))
    value >>= 7
    while value:
        encoded.insert(0, 0x80 | (value & 0x7F))
        value >>= 7
    return bytes(encoded)


def complete_suffix(name: str) -> str:
    """Qt ``QFileInfo::completeSuffix`` semantics: everything after the first
    ``.`` in the final path component (a leading dot for hidden files is not a
    separator). ``"model.tflite"`` -> ``"tflite"``, ``"a.tar.gz"`` -> ``"tar.gz"``.
    """
    base = os.path.basename(name)
    dot = base.find(".", 1)
    return base[dot + 1 :].lower() if dot != -1 else ""


def alignment_for(
    name: str,
    rules: Iterable[Mapping[str, object]],
    default: int = ROMFS_MIN_ALIGNMENT,
) -> int:
    """Alignment for ``name`` from ``rules`` (``[{extension, alignment}, ...]``),
    matched on the complete suffix. Falls back to ``default``."""
    suffix = complete_suffix(name)
    for rule in rules:
        if suffix == str(rule["extension"]).lower():
            return int(rule["alignment"])
    return default


# --- Writer -----------------------------------------------------------------

class VfsRomWriter:
    """Builds an OpenMV ROMFS image from directory/file calls.

    Usage mirrors the IDE: ``opendir``/``closedir`` to nest, ``mkfile`` for
    files, ``finalize`` once to get the image bytes. ``alignment_rules`` is a
    list of ``{"extension": str, "alignment": int}`` (typically a board
    partition's rules).
    """

    def __init__(
        self,
        alignment_rules: Iterable[Mapping[str, object]] | None = None,
        default_alignment: int = ROMFS_MIN_ALIGNMENT,
    ):
        self._rules = list(alignment_rules or [])
        self._default_alignment = max(ROMFS_MIN_ALIGNMENT, int(default_alignment))
        # Stack of [name, bytearray-of-child-records]; index 0 is the root.
        self._dirstack: list[list] = [["", bytearray()]]
        # Absolute byte offset of the current directory's next child. The root
        # starts at the header alignment because the header record wraps it.
        self._offsetstack: list[int] = [ROMFS_HEADER_ALIGNMENT]
        self._max_alignment = self._default_alignment
        for rule in self._rules:
            self._max_alignment = max(self._max_alignment, int(rule["alignment"]))

    # -- record encoders (private) --

    def _encode_record(
        self, kind: int, payload: bytes, alignment: int = 0, offset: int = 0, padding: int = 0
    ) -> bytes:
        if alignment:
            # Offset of the payload, accounting for the kind and size fields.
            offset += len(encode_uint(kind)) + len(encode_uint(len(payload)))
            padding = ((offset + (alignment - 1)) & ~(alignment - 1)) - offset
        kindb = encode_uint(kind) if kind else b""
        return kindb + bytes((_PADDING_BYTE,)) * padding + encode_uint(len(payload)) + payload

    def _encode_file(self, filename: str, payload: bytes, alignment: int, offset: int) -> bytes:
        name_record = self._encode_record(ROMFS_RECORD_KIND_UNUSED, to_ascii(filename))
        # Account for the outer FILE record's kind byte + worst-case alignment
        # padding + the name record, so the data payload's alignment is computed
        # from its real position. (Matches romfs.cpp::encodefile.)
        offset += 1 + (ROMFS_FILEREC_ALIGNMENT - 1) + len(name_record)
        data_record = self._encode_record(
            ROMFS_RECORD_KIND_DATA_VERBATIM, payload, alignment, offset
        )
        return self._encode_record(
            ROMFS_RECORD_KIND_FILE, name_record + data_record, ROMFS_FILEREC_ALIGNMENT
        )

    # -- public API --

    def opendir(self, dirname: str) -> None:
        self._dirstack.append([dirname, bytearray()])
        self._offsetstack.append(0)

    def closedir(self) -> None:
        if len(self._dirstack) < 2:
            raise RuntimeError("closedir without a matching opendir")
        dirname, content = self._dirstack.pop()
        self._offsetstack.pop()
        name_record = self._encode_record(ROMFS_RECORD_KIND_UNUSED, to_ascii(dirname))
        record = self._encode_record(
            ROMFS_RECORD_KIND_DIRECTORY,
            name_record + bytes(content),
            self._max_alignment,
            self._offsetstack[-1] + len(name_record),
        )
        self._offsetstack[-1] += len(record)
        self._dirstack[-1][1].extend(record)

    def mkfile(self, filename: str, filedata: bytes) -> None:
        alignment = alignment_for(filename, self._rules, self._default_alignment)
        record = self._encode_file(filename, filedata, alignment, self._offsetstack[-1])
        self._offsetstack[-1] += len(record)
        self._dirstack[-1][1].extend(record)

    def finalize(self) -> bytes:
        if len(self._dirstack) != 1:
            raise RuntimeError("finalize with %d unclosed director%s"
                               % (len(self._dirstack) - 1,
                                  "y" if len(self._dirstack) == 2 else "ies"))
        content = self._dirstack[0][1]
        return self._encode_record(
            ROMFS_HEADER_KIND, bytes(content), ROMFS_HEADER_ALIGNMENT
        )


# --- Reader -----------------------------------------------------------------

class RomfsError(ValueError):
    """Raised when ROMFS bytes are malformed."""


class _Entry:
    __slots__ = ("name", "is_dir", "data", "data_offset", "children")

    def __init__(self, name, is_dir):
        self.name = name
        self.is_dir = is_dir
        self.data: bytes | None = None
        # Absolute byte offset of this file's payload within the image (files only).
        self.data_offset: int | None = None
        self.children: list[_Entry] = []


class VfsRomReader:
    """Parses an OpenMV ROMFS image into a tree of entries.

    ``entries`` is the list of top-level entries; directories nest via
    ``entry.children``; files carry ``entry.data``. Faithful to
    ``romfs.cpp::VfsRomReader`` including ``DATA_POINTER`` resolution.
    """

    def __init__(self, data: bytes):
        self._data = bytes(data)
        self._end = len(self._data)
        self.entries: list[_Entry] = []

        if self._end < ROMFS_SIZE_MIN:
            raise RomfsError("image too small")
        if self._data[:3] != ROMFS_HEADER_MAGIC:
            raise RomfsError("bad ROMFS magic %r" % (self._data[:3],))

        pos = 0
        _, pos = self._decode_uint(pos)          # header kind (== magic)
        size, pos = self._decode_uint(pos)       # root payload size
        root_end = min(pos + size, self._end)
        self.entries = self._parse_dir(pos, root_end)

    # -- primitives --

    def _decode_uint(self, pos: int) -> tuple[int, int]:
        value = 0
        while True:
            if pos >= self._end:
                raise RomfsError("truncated integer")
            byte = self._data[pos]
            pos += 1
            value = (value << 7) | (byte & 0x7F)
            if not (byte & 0x80):
                return value, pos

    def _extract_record(self, pos: int) -> tuple[int, int, int]:
        """Return (kind, payload_start, payload_end)."""
        kind, pos = self._decode_uint(pos)
        length, pos = self._decode_uint(pos)
        return kind, pos, pos + length

    # -- recursive descent --

    def _parse_dir(self, start: int, stop: int) -> list[_Entry]:
        entries: list[_Entry] = []
        pos = start
        while pos < stop:
            if pos >= self._end:  # pragma: no cover - defensive; stop <= end
                break
            kind, body, nxt = self._extract_record(pos)
            if kind in (ROMFS_RECORD_KIND_DIRECTORY, ROMFS_RECORD_KIND_FILE):
                namelen, body = self._decode_uint(body)
                name = self._data[body : body + namelen].decode("latin-1")
                body += namelen
                if kind == ROMFS_RECORD_KIND_DIRECTORY:
                    entry = _Entry(name, is_dir=True)
                    entry.children = self._parse_dir(body, min(nxt, self._end))
                    entries.append(entry)
                else:
                    entry = _Entry(name, is_dir=False)
                    entry.data, entry.data_offset = self._read_file_data(body)
                    entries.append(entry)
            pos = nxt
        return entries

    def _read_file_data(self, pos: int) -> tuple[bytes, int]:
        """Return ``(payload_bytes, absolute_payload_offset)``."""
        datakind, body, tmp = self._extract_record(pos)
        payloadlen = tmp - body
        if datakind == ROMFS_RECORD_KIND_DATA_VERBATIM:
            data = self._data[body : body + payloadlen] if payloadlen > 0 else b""
            return data, body
        if datakind == ROMFS_RECORD_KIND_DATA_POINTER and payloadlen >= 8:
            dp_size, body = self._decode_uint(body)
            dp_data, body = self._decode_uint(body)
            return self._data[dp_data : dp_data + dp_size], dp_data
        return b"", body

    # -- convenience --

    def walk(self):
        """Yield ``(path, entry)`` for every entry, depth-first, POSIX paths."""
        def rec(entries, prefix):
            for e in entries:
                path = prefix + "/" + e.name if prefix else e.name
                yield path, e
                if e.is_dir:
                    yield from rec(e.children, path)
        yield from rec(self.entries, "")

    def extract(self, dest: str) -> int:
        """Write the tree to ``dest`` on disk. Returns the file count."""
        count = 0
        for path, entry in self.walk():
            target = os.path.join(dest, *path.split("/"))
            if entry.is_dir:
                os.makedirs(target, exist_ok=True)
            else:
                os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
                with open(target, "wb") as f:
                    f.write(entry.data or b"")
                count += 1
        return count

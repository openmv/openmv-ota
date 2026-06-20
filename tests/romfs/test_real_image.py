"""Ground-truth test: our writer reproduces a real OpenMV-IDE-built image
byte-for-byte, and our reader parses it.

``OPENMV2_romfs0.img`` was produced by the OpenMV IDE. Reading it and repacking
it (preserving record order) with the board's alignment rules must yield the
exact same bytes — this validates the format port against real output, not just
against our own reader.
"""

from __future__ import annotations

import os

from openmv_ota.romfs import boards as boards_mod
from openmv_ota.romfs.container import VfsRomReader, VfsRomWriter, alignment_for

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "OPENMV2_romfs0.img")


def _repack(reader, rules):
    w = VfsRomWriter(rules)

    def emit(entries):
        for e in entries:
            if e.is_dir:
                w.opendir(e.name)
                emit(e.children)
                w.closedir()
            else:
                w.mkfile(e.name, e.data or b"")

    emit(reader.entries)
    return w.finalize()


def test_reader_parses_real_image():
    raw = open(FIXTURE, "rb").read()
    reader = VfsRomReader(raw)
    names = sorted(p for p, e in reader.walk() if not e.is_dir)
    assert names == [
        "haarcascade_eye.cascade",
        "haarcascade_frontalface.cascade",
        "haarcascade_smile.cascade",
    ]
    # Every file has non-empty data.
    for _, e in reader.walk():
        if not e.is_dir:
            assert e.data


def test_writer_reproduces_real_image_byte_for_byte():
    raw = open(FIXTURE, "rb").read()
    rules = boards_mod.get_board("OPENMV2").partition(0).alignment_rules
    reader = VfsRomReader(raw)
    assert _repack(reader, rules) == raw


def test_real_image_payloads_are_aligned():
    raw = open(FIXTURE, "rb").read()
    rules = boards_mod.get_board("OPENMV2").partition(0).alignment_rules
    reader = VfsRomReader(raw)
    for _, e in reader.walk():
        if e.is_dir or not e.data:
            continue
        a = alignment_for(e.name, rules)
        off = raw.find(e.data)
        assert off >= 0 and off % a == 0

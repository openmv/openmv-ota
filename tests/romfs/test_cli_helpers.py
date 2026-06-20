"""Unit tests for the romfs CLI's small argument/format helpers."""

from __future__ import annotations

import argparse
import io

import pytest

from openmv_ota.romfs import cli


@pytest.mark.parametrize(
    "text, expected",
    [
        ("0", 0),
        ("1024", 1024),
        ("0x100", 256),
        ("4k", 4096),
        ("2K", 2048),
        ("1m", 1024**2),
        ("1M", 1024**2),
        ("1g", 1024**3),
    ],
)
def test_parse_size(text, expected):
    assert cli._parse_size(text) == expected


def test_parse_size_invalid():
    with pytest.raises(argparse.ArgumentTypeError):
        cli._parse_size("notasize")


@pytest.mark.parametrize(
    "value, ext, alignment",
    [
        ("tflite=32", "tflite", 32),
        ("bin:16", "bin", 16),
        (".onnx=64", "onnx", 64),
        ("TFLITE=0x80", "tflite", 128),
    ],
)
def test_parse_align_ok(value, ext, alignment):
    assert cli._parse_align(value) == {"extension": ext, "alignment": alignment}


@pytest.mark.parametrize(
    "value",
    [
        "noseparator",     # missing '=' or ':'
        "=32",             # empty extension
        "bin=abc",         # non-integer alignment
        "bin=24",          # not a power of two
        "bin=0",           # < 1
    ],
)
def test_parse_align_errors(value):
    with pytest.raises(argparse.ArgumentTypeError):
        cli._parse_align(value)


@pytest.mark.parametrize(
    "n, text",
    [
        (0, "0 B"),
        (512, "512 B"),
        (1024, "1.00 KiB"),
        (1536, "1.50 KiB"),
        (1024**2, "1.00 MiB"),
        (1024**3, "1.00 GiB"),
        (5 * 1024**3, "5.00 GiB"),
    ],
)
def test_human(n, text):
    assert cli._human(n) == text


def test_read_image_bytes_from_stdin(monkeypatch):
    import types
    monkeypatch.setattr(cli.sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(b"ABC")))
    assert cli._read_image_bytes("-") == b"ABC"


def test_read_image_bytes_from_file(tmp_path):
    p = tmp_path / "img.bin"
    p.write_bytes(b"\x01\x02")
    assert cli._read_image_bytes(str(p)) == b"\x01\x02"

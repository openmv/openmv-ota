"""The pure dfu-util argv builders -- the 'particular offsets/alts' under test."""

from __future__ import annotations

from pathlib import Path

from openmv_ota.flash import dfu


def test_download_argv_leaves_by_default():
    argv = dfu.download_argv("dfu-util", "37c5:9204", 3, Path("/b/OPENMV4-romfs.img"))
    assert argv == ["dfu-util", "-d", "37c5:9204", "-a", "3",
                    "-D", "/b/OPENMV4-romfs.img", "-s", ":leave"]


def test_download_argv_no_leave():
    argv = dfu.download_argv("/sdk/dfu-util", "37c5:9206", 1, Path("fw.bin"), leave=False)
    assert argv == ["/sdk/dfu-util", "-d", "37c5:9206", "-a", "1", "-D", "fw.bin"]


def test_upload_argv():
    argv = dfu.upload_argv("dfu-util", "37c5:9204", 2, Path("dump.bin"))
    assert argv == ["dfu-util", "-d", "37c5:9204", "-a", "2", "-U", "dump.bin"]

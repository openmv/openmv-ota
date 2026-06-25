"""Host tests for the device-side ``openmv_ota`` runtime library (the package
scaffolded into a project's ``app/lib/openmv_ota/``).

Like ``test_device_boot`` for ``boot.py``, this exercises the pure logic on the
host and pins the duplicated status-marker constants against the originals in
``openmv_ota.ota.status`` so they can't drift. The device entry points
(``status``/``confirm``/``sync``) wire MicroPython-only I/O and are covered under
QEMU, not here.
"""

from __future__ import annotations

from openmv_ota.build.device import openmv_ota as rt
from openmv_ota.ota import status as host_status


def _sector(pending, tried, confirmed):
    return host_status.build_status_sector(4096, pending=pending, tried=tried,
                                           confirmed=confirmed)


def test_markers_and_offsets_pinned_to_host():
    # The library duplicates these from openmv_ota.ota.status; keep them identical.
    assert (rt.PENDING, rt.TRIED, rt.CONFIRMED) == (
        host_status.PENDING, host_status.TRIED, host_status.CONFIRMED)
    assert (rt._PENDING_OFF, rt._TRIED_OFF, rt._CONFIRMED_OFF) == (
        host_status.PENDING_OFFSET, host_status.TRIED_OFFSET, host_status.CONFIRMED_OFFSET)
    assert rt.MARKER_SIZE == host_status.MARKER_SIZE


def test_status_of_confirmed_image():
    s = rt._status_of(_sector(True, True, True))   # post-OTA confirmed / factory shape
    assert s == {"pending": True, "tried": True, "confirmed": True, "trial": False}
    assert rt._needs_confirm(_sector(True, True, True)) is False


def test_status_of_unconfirmed_trial():
    s = rt._status_of(_sector(True, True, False))  # booted a one-shot trial, not yet kept
    assert s["trial"] is True and s["confirmed"] is False
    assert rt._needs_confirm(_sector(True, True, False)) is True


def test_status_of_pending_only_is_not_a_trial():
    # staged but not yet trial-booted (boot.py hasn't armed 'tried') -> nothing to confirm
    s = rt._status_of(_sector(True, False, False))
    assert s["trial"] is False
    assert rt._needs_confirm(_sector(True, False, False)) is False


def test_status_of_erased_sector():
    s = rt._status_of(_sector(False, False, False))
    assert s == {"pending": False, "tried": False, "confirmed": False, "trial": False}


def test_markers_decodes_each_flag():
    assert rt._markers(_sector(True, False, True)) == (True, False, True)


def test_resources_to_apply_filters_unchanged():
    manifest = [
        {"file": "a.romfs", "bytes": b"AAAA"},   # differs -> apply
        {"file": "b.romfs", "bytes": b"BBBB"},   # same    -> skip
    ]
    current = {"a.romfs": b"ZZZZ", "b.romfs": b"BBBB"}
    todo = rt._resources_to_apply(manifest, lambda e: current[e["file"]])
    assert [e["file"] for e in todo] == ["a.romfs"]


def test_resources_to_apply_missing_target_counts_as_changed():
    manifest = [{"file": "a.romfs", "bytes": b"AAAA"}]
    todo = rt._resources_to_apply(manifest, lambda e: None)   # unreadable target
    assert [e["file"] for e in todo] == ["a.romfs"]

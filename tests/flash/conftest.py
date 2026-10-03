"""Flash tests run with no cameras attached by default, so device discovery is
deterministic and never touches real serial ports. Tests that exercise discovery/reset
override ``device._comports`` themselves."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_cameras(monkeypatch):
    # stub the underlying pyserial scan (not device._comports), so the test that checks the
    # _comports wrapper itself can still re-stub list_ports
    monkeypatch.setattr("serial.tools.list_ports.comports", lambda: [])
    monkeypatch.setattr("openmv_ota.flash.flash.time.sleep", lambda _s: None)
    # every DFU wait finds its device at once; a test about the wait itself re-stubs this
    monkeypatch.setattr("openmv_ota.flash.flash._dfu_listed", lambda tool, usb: True)

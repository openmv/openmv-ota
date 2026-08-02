"""Arduino MCUboot boards enter DFU by 1200-baud touch ONLY -- never by catching a reset window.

The OpenMV bootloader presents a brief DFU window on every reset, so `dfu_reset_catch` starts a
`dfu-util -w` and pulses nRST to catch it. MCUboot has no such window: the pulse just reboots the
app while `-w` waits for a bootloader id that never enumerates. That is not a slow failure, it is
an unbounded one -- and it is silent, because the board stays alive and keeps logging.

Measured on the bench: `flash erase --in-bootloader` behind an nRST pulse left
`dfu-util -w -d ,2341:035b` waiting 11 minutes while the Portenta sat enumerated as 2341:045b (its
runtime CDC). The job never reached its publish step, so the device check-ins that WERE happening
found no release -- a hang that reads on the log like an OTA/offer bug.

These tests pin the entry rules, because every symptom of breaking them points somewhere else.
"""

import inspect
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_CIHIL = os.path.abspath(os.path.join(_HERE, "..", "..", "ci", "hil"))
sys.path.insert(0, _CIHIL)
os.environ.setdefault("WIFI_SSID", "")
os.environ.setdefault("WIFI_PASSWORD", "")

import ota_cycle  # noqa: E402  (ci/hil, added to sys.path above)

_SRC = open(os.path.join(_CIHIL, "ota_cycle.py")).read()
_ARDUINO = [b for b, c in ota_cycle.BOARDS.items() if c.get("flash") == "arduino_cli"]


def test_there_are_arduino_boards_to_protect():
    assert _ARDUINO, "expected at least one arduino_cli board (Nicla/Portenta)"


@pytest.mark.parametrize("board", _ARDUINO)
def test_dfu_reset_catch_refuses_arduino_boards(board, monkeypatch):
    """The wrong primitive must be impossible to use by accident, not merely discouraged."""
    called = []
    monkeypatch.setattr(ota_cycle, "jlink_reset_pulse", lambda b: called.append(b))
    with pytest.raises(RuntimeError, match="no DFU window|wrong primitive"):
        ota_cycle.dfu_reset_catch(board, ["dfu-util"])
    assert not called, "must refuse BEFORE pulsing the reset line"


@pytest.mark.parametrize("board", _ARDUINO)
def test_erase_recovery_never_pulses_reset_on_arduino(board, monkeypatch):
    """recover_erase_romfs is the call that hung. On an Arduino it must take the touch path."""
    monkeypatch.setattr(ota_cycle, "_dfu_present", lambda: False)
    monkeypatch.setattr(os.path, "exists", lambda p: True)          # a runtime port is enumerated
    pulses, ran = [], []
    monkeypatch.setattr(ota_cycle, "jlink_reset_pulse", lambda b: pulses.append(b))
    monkeypatch.setattr(ota_cycle, "sh",
                        lambda cmd, **kw: (ran.append(cmd), (0, ""))[1])
    assert ota_cycle.recover_erase_romfs(board) is True
    assert not pulses, "no nRST on an Arduino -- it cannot produce a DFU window"
    assert ran, "expected the erase command to actually run"
    assert "--in-bootloader" not in ran[-1], (
        "not in DFU yet -- the CLI must do its own 1200-baud touch, not be told to skip it")


@pytest.mark.parametrize("board", _ARDUINO)
def test_already_in_dfu_skips_the_touch(board, monkeypatch):
    """A board sitting in DFU has no CDC to touch; write to it directly instead of waiting."""
    monkeypatch.setattr(ota_cycle, "_dfu_present", lambda: True)
    ran = []
    monkeypatch.setattr(ota_cycle, "sh", lambda cmd, **kw: (ran.append(cmd), (0, ""))[1])
    rc, _ = ota_cycle._arduino_dfu_run(board, ["openmv-ota", "flash"], "t", timeout=5)
    assert rc == 0 and "--in-bootloader" in ran[-1]


@pytest.mark.parametrize("board", _ARDUINO)
def test_no_dfu_and_no_port_fails_fast(board, monkeypatch):
    """Neither route available -> return an error NOW. Waiting cannot conjure a bootloader, and a
    wait here is exactly what burned the 11 minutes."""
    monkeypatch.setattr(ota_cycle, "_dfu_present", lambda: False)
    monkeypatch.setattr(os.path, "exists", lambda p: False)
    monkeypatch.setattr(ota_cycle, "sh",
                        lambda *a, **k: pytest.fail("must not run a dfu-util command"))
    rc, out = ota_cycle._arduino_dfu_run(board, ["openmv-ota"], "t", timeout=5)
    assert rc != 0 and "power cycle" in out


def test_dfu_present_never_waits():
    """`-l` lists what is enumerated now; a `-w` here would reintroduce the unbounded wait."""
    body = _SRC.split("def _dfu_present(")[1].split("\ndef ")[0]
    assert '"-l"' in body and '"-w"' not in body


def test_arduino_flash_does_not_use_the_reset_window():
    body = _SRC.split("def _flash_arduino_cli(")[1].split("\ndef ")[0]
    assert "dfu_reset_catch" not in body, "MCUboot has no reset DFU window to catch"
    assert "_arduino_dfu_run" in body

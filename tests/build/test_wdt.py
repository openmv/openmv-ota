"""Host tests for the device OTA watchdog helper (device/openmv_wdt.py, frozen as
openmv_wdt).

Loaded as a file module (under the openmv_ota name) so coverage measures it. Only the
disabled paths are reachable off-device -- the machine.WDT/Timer wiring is device-only
(``pragma: no cover``); on real hardware it's exercised by the app + the installer.
"""

import importlib.util
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src/openmv_ota/build/device/openmv_wdt.py"
_spec = importlib.util.spec_from_file_location("openmv_ota._wdt_under_test", str(_SRC))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_disabled_by_default():
    assert _mod.ENABLED is False
    assert _mod._wdt is None and _mod._timer is None and _mod._feed is None


def test_timer_id_is_the_soft_timer():
    # machine.Timer on every OpenMV port is the soft timer; only -1 is accepted, and the
    # feed must run at interrupt time (hard=True) to fire during a blocking erase.
    assert _mod.TIMER_ID == -1


def test_feed_is_a_noop_when_disabled():
    # _wdt is None -> feed() does nothing and never touches machine
    assert _mod.feed() is None


def test_start_is_a_noop_when_disabled():
    # ENABLED is False -> start() must NOT arm the watchdog (nothing to reset an app that
    # never turned it on); _wdt stays None so feed()/relax() stay no-ops too.
    _mod.start()
    assert _mod._wdt is None


def test_relax_is_a_noop_context_when_disabled():
    # no watchdog -> relax() enters/exits without starting a timer
    with _mod.relax():
        pass
    assert _mod._timer is None


# --- the stm32 IWDG guard ---------------------------------------------------
# The IWDG cannot be stopped by software and survives reset, romfs erase AND reflash -- only a
# power cycle clears it, so arming it on a device that might starve it means someone has to go
# physically unplug the board. These pin the guard that keeps that from happening by accident.

class _FakeUname:
    def __init__(self, machine):
        self.machine = machine


def _as_board(monkeypatch, machine_str):
    import os
    monkeypatch.setattr(os, "uname", lambda: _FakeUname(machine_str), raising=False)


def test_stm32_iwdg_is_refused_by_default():
    assert _mod.ALLOW_STM32_IWDG is False        # the safe default is what protects a field device


@pytest.mark.parametrize("wdt_id", [0, "IWDG"])
def test_reject_stm32_iwdg_raises_on_stm32(monkeypatch, wdt_id):
    _as_board(monkeypatch, "OpenMV H7 Plus with STM32H743")
    with pytest.raises(ValueError, match="POWER CYCLE"):   # the message must say how to recover
        _mod._reject_stm32_iwdg(wdt_id, "test")


def test_reject_stm32_iwdg_allows_non_stm32(monkeypatch):
    # mimxrt/alif WDT(0) is the deep-sleep-safe WDOG / alif WDT, NOT an IWDG -- never blocked
    _as_board(monkeypatch, "OpenMV RT1060 with MIMXRT1062")
    assert _mod._reject_stm32_iwdg(0, "test") is None
    _as_board(monkeypatch, "OpenMV AE3 with ALIF")
    assert _mod._reject_stm32_iwdg(0, "test") is None


def test_reject_stm32_iwdg_honours_the_opt_in(monkeypatch):
    # the escape hatch stays available for an app that never deep-sleeps and can guarantee feeding
    _as_board(monkeypatch, "OpenMV H7 Plus with STM32H743")
    monkeypatch.setattr(_mod, "ALLOW_STM32_IWDG", True)
    assert _mod._reject_stm32_iwdg(0, "test") is None

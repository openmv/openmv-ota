"""`_ensure_cdc` must not probe a board whose app has armed its watchdog (ci/hil/ota_cycle.py).

OPENMV4P watchdog leg, PR #130, two runs in a row: 15 s after the golden flash the post-flash
`_ensure_cdc` asked `mpremote eval True`; the Ctrl-C landed in the bench app as
`app: CRASHED KeyboardInterrupt()`, the feed stopped, the 100 ms watchdog bit, and the next boot
logged `app: woke from a watchdog reset` -- the marker the scenario forbids. Each further probe of
each further attempt did the same. The board was healthy the whole time and said so on its UART.
"""

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..", "..", "ci", "hil")))
os.environ.setdefault("WIFI_SSID", "")
os.environ.setdefault("WIFI_PASSWORD", "")

import ota_cycle  # noqa: E402


class _Cap:
    def __init__(self, raw):
        self.raw = raw


def test_wdt_armed_since_flash_is_bounded_by_the_flash_mark(monkeypatch):
    lines = ["INFO openmv_ota: wdt: armed 100ms", "boot: ready", "app: wdt armed=True"]
    monkeypatch.setattr(ota_cycle, "_CAP", _Cap(lines))
    monkeypatch.setattr(ota_cycle, "_FLASH_MARK", 0)
    assert ota_cycle._wdt_armed_since_flash() is True
    monkeypatch.setattr(ota_cycle, "_FLASH_MARK", 1)       # the arming belongs to the PREVIOUS firmware
    assert ota_cycle._wdt_armed_since_flash() is False
    monkeypatch.setattr(ota_cycle, "_CAP", None)
    assert ota_cycle._wdt_armed_since_flash() is False


def test_ensure_cdc_leaves_an_armed_board_alone(monkeypatch):
    monkeypatch.setattr(ota_cycle, "_wdt_armed_since_flash", lambda: True)
    monkeypatch.setattr(ota_cycle, "_cdc_answers", lambda: pytest.fail("the probe kills the feed"))
    monkeypatch.setattr(ota_cycle, "sh", lambda *a, **k: pytest.fail("no fuser, no J-Link"))
    monkeypatch.setattr(ota_cycle, "jlink_reset_pulse", lambda b: pytest.fail("no reset"))
    logs = []
    monkeypatch.setattr(ota_cycle, "log", logs.append)
    assert ota_cycle._ensure_cdc("OPENMV4P", allow_erase=True) is None
    assert any("armed its watchdog" in m for m in logs)


def test_ensure_cdc_still_probes_when_nothing_is_armed(monkeypatch):
    monkeypatch.setattr(ota_cycle, "_wdt_armed_since_flash", lambda: False)
    asked = []
    monkeypatch.setattr(ota_cycle, "_cdc_answers", lambda: asked.append(1) or True)
    monkeypatch.setattr(ota_cycle, "log", lambda m: None)
    ota_cycle._ensure_cdc("OPENMV4P")
    assert asked == [1]


def _armed_after_the_first_probe(monkeypatch):
    answers = iter([False, True, True, True])      # at the top: not yet; after a probe: re-armed
    monkeypatch.setattr(ota_cycle, "_wdt_armed_since_flash", lambda: next(answers))
    asked, pulses, erased = [], [], []
    monkeypatch.setattr(ota_cycle, "_cdc_answers", lambda: asked.append(1) or False)
    monkeypatch.setattr(ota_cycle, "sh", lambda *a, **k: None)
    monkeypatch.setattr(ota_cycle, "_board_absent", lambda: False)
    monkeypatch.setattr(ota_cycle, "jlink_reset_pulse", lambda b: pulses.append(b) or True)
    monkeypatch.setattr(ota_cycle.time, "sleep", lambda s: None)
    monkeypatch.setattr(ota_cycle, "recover_erase_romfs", lambda b: erased.append(b))
    logs = []
    monkeypatch.setattr(ota_cycle, "log", logs.append)
    return asked, pulses, erased, logs


def test_pre_flash_recovery_erases_once_the_board_re_arms_after_a_probe(monkeypatch):
    asked, pulses, erased, logs = _armed_after_the_first_probe(monkeypatch)
    ota_cycle._ensure_cdc("OPENMV4P", allow_erase=True)
    assert asked == [1] and pulses == ["OPENMV4P"]     # one probe, one pulse, then no more
    assert erased == ["OPENMV4P"]                      # straight to the erase the flash needs
    assert any("no more REPL probes; erasing" in m for m in logs)


def test_post_flash_recovery_leaves_a_board_that_re_arms_after_a_probe(monkeypatch):
    asked, pulses, erased, logs = _armed_after_the_first_probe(monkeypatch)
    assert ota_cycle._ensure_cdc("OPENMV4P") is None
    assert asked == [1] and pulses == ["OPENMV4P"]
    assert erased == []                                # never post-flash: it would wipe golden
    assert any("no more REPL probes" in m and "erasing" not in m for m in logs)

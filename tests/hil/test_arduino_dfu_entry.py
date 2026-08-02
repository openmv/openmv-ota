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


def _body(name):
    """A function's source, docstring stripped -- these helpers document the very hazards they
    avoid ("an mpremote probe kills the app"), so scanning the raw text matches the prose and not
    the behaviour. Assert against what the function DOES."""
    src = _SRC.split("def %s(" % name)[1].split("\ndef ")[0]
    while '"""' in src:                      # drop each docstring//comment block in turn
        head, _, rest = src.partition('"""')
        _, _, tail = rest.partition('"""')
        src = head + tail
    return "\n".join(ln.split("#", 1)[0] for ln in src.splitlines())


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
    body = _body("_dfu_present")
    assert '"-l"' in body and '"-w"' not in body


def test_arduino_flash_does_not_use_the_reset_window():
    body = _body("_flash_arduino_cli")
    assert "dfu_reset_catch" not in body, "MCUboot has no reset DFU window to catch"
    assert "_arduino_dfu_run" in body


# ---------------------------------------------------------------------------
# Boot-time budget. Measured on the bench (J-Link reset -> /dev/ttyACM0):
_PORTENTA_BOOT_S = 31.7
# The old code slept a flat 15 s after a factory flash and then retried every ~11 s WITH A RESET in
# the loop, so a healthy board never finished booting once. The run failed with "golden did not
# mount a valid romfs" against a board that was running that exact golden minutes later.


def test_await_cdc_budget_clears_the_measured_boot():
    """The budget must exceed the measured boot with real margin, not sit near it."""
    budget = inspect.signature(ota_cycle._await_cdc).parameters["budget"].default
    assert budget > 3 * _PORTENTA_BOOT_S, (
        "budget %ss leaves no margin over a %ss boot" % (budget, _PORTENTA_BOOT_S))


def test_await_cdc_returns_as_soon_as_the_board_answers(monkeypatch):
    """Costs nothing on a healthy board: no waiting once it responds."""
    monkeypatch.setattr(ota_cycle, "_cdc_responsive", lambda *a, **k: True)
    monkeypatch.setattr(ota_cycle.time, "sleep",
                        lambda s: pytest.fail("must not sleep once the board answers"))
    assert ota_cycle._await_cdc("ARDUINO_PORTENTA_H7") is True


def test_await_cdc_gives_up_and_reports(monkeypatch):
    monkeypatch.setattr(ota_cycle, "_cdc_responsive", lambda *a, **k: False)
    monkeypatch.setattr(ota_cycle.time, "sleep", lambda s: None)
    assert ota_cycle._await_cdc("ARDUINO_PORTENTA_H7", budget=0) is False


def test_arduino_flash_never_sleeps_a_flat_guess():
    """A fixed sleep cannot cover a 33s boot; the wait must be driven by the board's own output."""
    assert "time.sleep(15)" not in _body("_flash_arduino_cli")


@pytest.mark.parametrize("board", _ARDUINO)
def test_ensure_cdc_waits_and_never_resets_an_arduino(board, monkeypatch):
    """Neither reset belongs here. The nRST PIN leaves the core halted with no USB at all; a CORE
    reset restarts a 33 s boot on a ~47 s retry cadence, so the port is absent for much of the
    window verify needs it -- three minutes of "mpremote: port busy" ending in "golden did not mount
    a valid romfs", against a golden that was fine. Waiting is the recovery."""
    monkeypatch.setattr(ota_cycle, "_cdc_responsive", lambda *a, **k: False)
    monkeypatch.setattr(ota_cycle, "_dfu_leave", lambda b: False)   # not in DFU -> the wait path
    monkeypatch.setattr(ota_cycle, "sh", lambda *a, **k: (1, ""))
    waits = []
    monkeypatch.setattr(ota_cycle, "_await_boot", lambda b, **k: waits.append(b) or False)
    monkeypatch.setattr(ota_cycle, "jlink_core_reset",
                        lambda *a, **k: pytest.fail("must not reset the core mid-boot"))
    monkeypatch.setattr(ota_cycle, "jlink_reset_pulse",
                        lambda *a, **k: pytest.fail("must not pulse the nRST pin on an Arduino"))
    ota_cycle._ensure_cdc(board)          # allow_erase defaults False -> returns after the retries
    assert waits, "expected the board to be given time to boot"


def test_core_reset_never_touches_the_reset_pin():
    """Kept as a manual-recovery primitive; if it is ever used again it must not touch the pin."""
    body = _body("jlink_core_reset")
    assert "SetRESET" not in body and "ClrRESET" not in body
    assert "connect" in body


def test_arduino_flash_watches_the_uart_and_does_not_probe():
    """The post-flash wait must be passive. An mpremote probe Ctrl-C's the app dead (KeyboardInterrupt
    is a BaseException the app's `except Exception` misses), which is what froze every boot at
    `data: path`."""
    body = _body("_flash_arduino_cli")
    assert "_await_boot" in body
    assert "_await_cdc" not in body, "the passive wait must replace the probe, not sit beside it"


def test_arduino_flash_resets_then_watches():
    """Both halves, in order. After `:leave` the board's UART goes silent and it never boots on its
    own (measured twice), so a debug-core reset is what starts it; and the wait that follows must be
    passive, because a probe Ctrl-C's the app dead. Fixing either alone still fails the run."""
    body = _body("_flash_arduino_cli")
    assert "jlink_core_reset" in body, "after :leave the board does not boot by itself"
    assert body.index("jlink_core_reset") < body.index("_await_boot"), "reset, THEN watch"


def test_await_boot_is_passive(monkeypatch):
    """It must never open the CDC: that is the whole point."""
    body = _body("_await_boot")
    assert "_cdc_responsive" not in body, "_await_boot must never probe the CDC itself"
    # only the no-capture fallback may delegate to the probing waiter, and it must be guarded
    assert body.index("_CAP is None") < body.index("_await_cdc")


def test_await_boot_ignores_a_previous_boots_lines(monkeypatch):
    """A marker already in the buffer is a STALE boot -- counting it would return instantly and skip
    the wait entirely."""
    class Cap:
        raw = ["boot: ready, running app"]        # left over from before the flash
    monkeypatch.setattr(ota_cycle, "_CAP", Cap())
    monkeypatch.setattr(ota_cycle.time, "sleep", lambda s: None)
    assert ota_cycle._await_boot("ARDUINO_PORTENTA_H7", budget=0) is False


def test_await_boot_sees_a_fresh_marker(monkeypatch):
    class Cap:
        raw = []
    cap = Cap()
    monkeypatch.setattr(ota_cycle, "_CAP", cap)
    monkeypatch.setattr(ota_cycle.time, "sleep",
                        lambda s: cap.raw.append("INFO openmv_ota: boot: ready, running app"))
    assert ota_cycle._await_boot("ARDUINO_PORTENTA_H7", budget=30) is True


def test_bench_app_reports_a_keyboardinterrupt_death():
    """The app must not die silently when the harness Ctrl-C's it -- that silence is what made a
    perfectly healthy board look hung."""
    src = ota_cycle.bench_main_py("ARDUINO_PORTENTA_H7", "wifi")
    assert "except BaseException" in src
    assert "app: CRASHED" in src

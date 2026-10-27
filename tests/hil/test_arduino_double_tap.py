"""The J-Link reset double-tap that revives an Arduino board gone from USB (ci/hil/ota_cycle.py).

HIL run 40444283788 on the Nicla: six scenarios green, then a DFU write failed partway, the CLI left
DFU to retry from a fresh entry, MCUboot ran the torn firmware and the board never enumerated again.
Nothing the harness had reached it -- a single reset (pin or core) re-runs the same torn image. Two
nRST pulses 350 ms apart are the bootloader's own double-tap gesture; they brought the board back as
a DFU device in two seconds, from exactly that state, on the first try (and did the same on the
Portenta and the Giga).
"""

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_CIHIL = os.path.abspath(os.path.join(_HERE, "..", "..", "ci", "hil"))
sys.path.insert(0, _CIHIL)
os.environ.setdefault("WIFI_SSID", "")
os.environ.setdefault("WIFI_PASSWORD", "")

import ota_cycle  # noqa: E402  (ci/hil, added to sys.path above)

_ARDUINO = sorted(b for b, c in ota_cycle.BOARDS.items() if c.get("flash") == "arduino_cli")


def _jlink(monkeypatch):
    """Capture the J-Link invocation and the commander script it was handed (read at call time:
    the file is unlinked right after)."""
    runs = []

    def sh(cmd, **k):
        if cmd[0] == "pkill":
            return 0, ""
        runs.append((cmd, open(cmd[cmd.index("-CommanderScript") + 1], "rb").read()))
        return 0, ""
    monkeypatch.setattr(ota_cycle, "sh", sh)
    monkeypatch.setitem(ota_cycle.CFG, "jlink", "JLinkExe")
    return runs


@pytest.mark.parametrize("board", _ARDUINO)
def test_double_tap_is_two_pin_pulses_with_no_core_connect(board, monkeypatch):
    runs = _jlink(monkeypatch)
    assert ota_cycle.jlink_double_tap(board) is True
    (cmd, script), = runs
    assert cmd[0] == "JLinkExe" and "-AutoConnect" in cmd and cmd[cmd.index("-AutoConnect") + 1] == "0"
    assert script.count(b"SetRESET") == 2 and script.count(b"ClrRESET") == 2
    assert b"connect" not in script and b"\nr\n" not in script, "pin only: the Nicla has no usable SWD"
    assert b"Sleep 350" in script, "the two taps must land inside the bootloader's double-tap window"


def test_double_tap_is_a_no_op_without_a_jlink(monkeypatch):
    monkeypatch.setitem(ota_cycle.BOARDS, "NO_JLINK", {"flash": "arduino_cli"})
    monkeypatch.setattr(ota_cycle, "sh", lambda *a, **k: pytest.fail("nothing to drive"))
    assert ota_cycle.jlink_double_tap("NO_JLINK") is False
    assert ota_cycle.arduino_revive("NO_JLINK") is False


def test_revive_returns_once_dfu_shows_up(monkeypatch):
    answers = iter([False, False, True])
    monkeypatch.setattr(ota_cycle, "jlink_double_tap", lambda b: True)
    monkeypatch.setattr(ota_cycle, "_dfu_present", lambda: next(answers))
    sleeps, logs = [], []
    monkeypatch.setattr(ota_cycle.time, "sleep", sleeps.append)
    monkeypatch.setattr(ota_cycle, "log", logs.append)
    assert ota_cycle.arduino_revive("ARDUINO_NICLA_VISION") is True
    assert sleeps == [2, 2, 2], "poll, do not wait the whole budget"
    assert any("back in DFU" in m for m in logs)


def test_revive_gives_up_at_the_budget(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(ota_cycle, "jlink_double_tap", lambda b: True)
    monkeypatch.setattr(ota_cycle, "_dfu_present", lambda: False)
    monkeypatch.setattr(ota_cycle.time, "time", lambda: clock[0])
    monkeypatch.setattr(ota_cycle.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    logs = []
    monkeypatch.setattr(ota_cycle, "log", logs.append)
    assert ota_cycle.arduino_revive("ARDUINO_NICLA_VISION", budget=6) is False
    assert clock[0] >= 1006 and any("no DFU device" in m for m in logs)


def test_pre_flash_absent_board_is_double_tapped_before_any_core_reset():
    """The flash pre-check: double-tap first, core reset + boot only as the fallback."""
    import inspect
    src = inspect.getsource(ota_cycle._flash_arduino_cli)
    body = "\n".join(line.split("#")[0] for line in src.splitlines())
    at = body.index("offers neither DFU nor a port")
    assert body.index("arduino_revive(board)", at) < body.index("jlink_core_reset(board)", at)

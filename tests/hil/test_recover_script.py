"""ci/hil/recover.py and the J-Link reaper it relies on must not kill what they are rescuing.

* ``_free_jlink`` ran ``pkill -f JLinkExe``, which matches full command lines -- including the
  documented ``JLINK=.../JLinkExe ./recover.py ...`` invocation, so it killed its own caller.
* ``--reset`` probed over mpremote first, and that Ctrl-C kills the running app before the pulse.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "ci", "hil"))

import ota_cycle  # noqa: E402
import recover  # noqa: E402


def test_free_jlink_kills_by_exact_process_name(monkeypatch):
    ran = []
    monkeypatch.setattr(ota_cycle, "sh", lambda cmd, **k: ran.append(cmd) or (0, ""))
    ota_cycle._free_jlink()
    assert ran == [["pkill", "-x", "JLinkExe"]]      # exact comm match, no shell, never -f


def _no_probe(monkeypatch):
    calls = []

    def sh(cmd, **k):
        if "mpremote" in " ".join(map(str, cmd)):
            raise AssertionError("the REPL probe ran -- its Ctrl-C kills the app")
        return 0, ""
    monkeypatch.setattr(recover.oc, "sh", sh)
    monkeypatch.setattr(recover.oc, "jlink_reset_pulse", lambda b: calls.append(("pulse", b)) or True)
    monkeypatch.setattr(recover.oc, "recover_firmware",
                        lambda b, img: calls.append(("firmware", b)) or True)
    return calls


def test_reset_pulses_without_probing_the_repl(monkeypatch):
    calls = _no_probe(monkeypatch)
    assert recover.main(["--board", "OPENMV_N6", "--reset"]) == 0
    assert calls == [("pulse", "OPENMV_N6")]


def test_firmware_reflash_skips_the_probe_too(monkeypatch):
    calls = _no_probe(monkeypatch)
    assert recover.main(["--board", "OPENMV_N6", "--firmware"]) == 0
    assert calls == [("firmware", "OPENMV_N6")]


def test_probe_still_probes(monkeypatch):
    seen = []
    monkeypatch.setattr(recover.oc, "sh", lambda cmd, **k: seen.append(cmd) or (0, ""))
    assert recover.main(["--board", "OPENMV_N6", "--probe"]) == 0
    assert any("eval" in c for c in seen)

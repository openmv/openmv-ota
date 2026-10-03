"""The romfs erase in `_ensure_cdc` is DESTRUCTIVE, and it is gated for a reason.

Erasing frees DFU on a board whose CDC is gone, which is the only lever left when the app itself
is what breaks the port. But it is only ever right BEFORE a flash: run post-flash it wipes the
golden image that was just written. That is not hypothetical -- a bench run flashed golden through
the bootloader's DFU window, found the CDC not yet back, and erased it again, so the scenario
failed with `golden did not mount a valid romfs` on a board that had just been provisioned.

These tests pin the gate and the call sites, because the failure is silent (a wiped board looks
exactly like a board that never flashed) and the blast radius is every scenario on every board.
"""

import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_CIHIL = os.path.abspath(os.path.join(_HERE, "..", "..", "ci", "hil"))
sys.path.insert(0, _CIHIL)
os.environ.setdefault("WIFI_SSID", "")
os.environ.setdefault("WIFI_PASSWORD", "")

import ota_cycle  # noqa: E402  (ci/hil, added to sys.path above)

_SRC = open(os.path.join(_CIHIL, "ota_cycle.py")).read()


def test_erase_is_opt_in():
    """Default OFF: a caller that hasn't thought about it must not destroy the board's image."""
    import inspect
    sig = inspect.signature(ota_cycle._ensure_cdc)
    assert sig.parameters["allow_erase"].default is False


def test_post_flash_ensure_cdc_never_erases():
    """_flash_dfu_cli's trailing recovery must NOT pass allow_erase -- it runs AFTER the write."""
    body = _SRC.split("def _flash_dfu_cli(")[1].split("\ndef ")[0]
    calls = re.findall(r"_ensure_cdc\([^)]*\)", body)
    assert calls, "expected _flash_dfu_cli to still recover the CDC"
    # the LAST call in the function is the post-flash one; it must not enable the erase
    assert "allow_erase" not in calls[-1], (
        "the post-flash _ensure_cdc must not erase -- it would wipe the golden just written: %r"
        % calls[-1])


def test_erase_path_is_reached_only_with_the_flag():
    """The destructive call must sit behind the flag, not before it."""
    body = _SRC.split("def _ensure_cdc(")[1].split("\ndef ")[0]
    guard = body.index("if not allow_erase:")
    erase = body.index("recover_erase_romfs(")
    assert guard < erase, "recover_erase_romfs must be gated by `if not allow_erase: return`"


def test_firmware_recovery_is_two_stage_and_zero_first():
    """A cycling board offers a window too short for a full image (measured: a direct write died at
    32%, then 36% of 2 MB). Writing a sector of zeros first invalidates the firmware, so the
    bootloader stops handing over and parks in DFU -- then the real write has all the time it needs.
    Order is the whole trick; a single-stage write here is the bug this replaced."""
    body = _SRC.split("def recover_firmware(")[1].split("\ndef ")[0]
    assert "stage 1/2" in body and "stage 2/2" in body
    assert body.index("stage 1/2") < body.index("stage 2/2")
    assert 'b"\\x00" * 4096' in body, "stage 1 must write a small sector of zeros"
    # stage 1 must go through the reset-catch (dfu-util -w started BEFORE the pulse)
    assert body.index("dfu_reset_catch") < body.index("stage 2/2")


def test_firmware_recovery_refuses_a_non_dfu_board():
    """The imx boards flash through their SBL, not DFU -- there is no firmware alt to write."""
    import ota_cycle as oc
    assert oc.recover_firmware("OPENMV_RT1060") is False


def test_partial_download_is_distinguished_from_never_started():
    """Only a download that died PARTWAY has corrupted the firmware. One that never began left the
    old image intact, and running a two-stage recovery there would destroy a working board."""
    import ota_cycle as oc
    partial = ("Download\t[========    ]  32%  647168 bytes"
               "dfu-util: Error during download get_status (LIBUSB_ERROR_IO)")
    assert oc._partial_download(partial) is True
    assert oc._partial_download("dfu-util: No DFU capable USB device available") is False
    assert oc._partial_download("") is False
    assert oc._partial_download(None) is False


def test_failed_dfu_flash_recovers_then_retries():
    """A partial write leaves firmware that guarantees the NEXT attempt fails the same way (32%,
    36%, 32% across three runs on the N6). The flash path must break that cycle, not repeat it."""
    body = _SRC.split("def _flash_dfu_cli(")[1].split("\ndef ")[0]
    assert "_partial_download(out)" in body
    assert "recover_firmware(board)" in body
    assert body.index("_partial_download(out)") < body.index("recover_firmware(board)")


def test_firmware_stage2_checks_the_park_instead_of_assuming_it(monkeypatch):
    """Stage 2 used a plain `dfu-util -w` on the assumption that stage 1 had parked the board. When
    it had not, that -w waited for a device that was never coming and burned the whole timeout
    (rc=124 after 400s on the N6). Check, and make a window when there isn't one."""
    body = _SRC.split("def recover_firmware(")[1].split("\ndef ")[0]
    stage2 = body.split("stage 2/2")[0]
    assert "_dfu_present()" in stage2, "stage 2 must verify the park before trusting a plain -w"
    assert "dfu_reset_catch" in body, "and must be able to MAKE a window when it is not parked"


def test_scored_window_resets_over_swd_when_a_jlink_exists():
    """Asking for the reset over mpremote takes the REPL with a Ctrl-C first. On a board whose app
    has ARMED THE WATCHDOG that stops the feed, so the watchdog bites before machine.reset() runs
    and the board boots with reset_cause==3. wdt_bite then reads that as "already bitten, recover"
    and skips the bite sequence -- the scenario fails with wdt.bit/wdt.stop missing. The harness was
    choosing the reset cause it was about to measure."""
    body = _SRC.split("def run_cycle(")[1].split("\ndef ")[0]
    # Strip comments and the docstring: this function EXPLAINS machine.reset() at length, and
    # matching that prose instead of the call is how the first version of this test failed.
    code = "\n".join(ln.split("#", 1)[0] for ln in body.splitlines())
    assert 'BOARDS[_BOARD].get("jlink_device")' in code, "every J-Link board must reset over SWD"
    assert code.index("jlink_core_reset") < code.index("machine.reset()"), "SWD first, REPL fallback"
    # and it must CONFIRM the reset: jlink_core_reset returns True for any board that HAS a J-Link,
    # so an ineffective reset is indistinguishable from a working one without watching for the boot
    assert "_await_boot(_BOARD" in code, "the SWD reset must be confirmed by the board's own boot"


def test_scored_window_reset_cascades_and_spares_the_arduino_pin():
    """Core reset -> (confirm) -> nRST pin -> (confirm) -> REPL. Each step is confirmed by the
    board's own boot marker, because a reset helper returning True proves only that the board HAS a
    J-Link. The pin step must skip the Arduino boards: there it can land the board back in its DFU
    bootloader (the touch's stay-in-bootloader flag lives in RAM and survives the pin)."""
    body = _SRC.split("def run_cycle(")[1].split("\ndef ")[0]
    code = "\n".join(ln.split("#", 1)[0] for ln in body.splitlines())
    assert code.index("jlink_core_reset") < code.index("jlink_reset_pulse") < code.index("machine.reset()")
    pin = code[code.index("jlink_reset_pulse") - 400:code.index("jlink_reset_pulse")]
    assert 'flash") != "arduino_cli"' in pin, "the pin step must exclude the Arduino boards"



def test_jlink_helpers_free_a_stale_probe_first():
    """A J-Link is single-client: one leftover JLinkExe makes every later connect fail, silently --
    sh(check=False) returns, the helper reports success, and the board is never reset. That is why
    the N6's SWD reset works when watchdog_bite runs ALONE and fails after nine prior scenarios."""
    for fn in ("jlink_core_reset", "jlink_reset_pulse"):
        body = _SRC.split("def %s(" % fn)[1].split("\ndef ")[0]
        assert "_free_jlink()" in body, fn
        assert body.index("_free_jlink()") < body.index("CommanderScript"), fn

# The three tests that used to live here pinned the DEFERRED /flash bench-file write: the CDC
# poll, the "never silently skip" guard, and the reset that made the firmware re-read the file.
# All of that machinery is gone -- the harness no longer writes /flash at all. Both bench files
# ride in the romfs, so they are on the board the moment it boots. What replaced them is the
# single invariant below.

def test_the_harness_never_writes_to_flash():
    """/flash is CORRUPTIBLE -- a cancelled run has wedged the mimxrt's, and a watchdog bite left
    the N6's missing the CA (161 ENOENT deaths, no check-in). Everything the HIL needs on the
    board now rides in the romfs the golden flash writes.

    Deleting the writes deleted their whole tail of complexity with them: the CDC wait, the
    reset-to-re-read, the USB-MSC drop-in, the `flash erase` self-heal, and -- the one that cost
    real debugging -- the REPL fallback, whose Ctrl-C KILLS a running app and produced the
    `CRASHED KeyboardInterrupt()` seen inside scored windows.
    """
    import inspect

    import ota_cycle

    for fn in (ota_cycle.prepare, ota_cycle.flash_golden):
        src = inspect.getsource(fn)
        assert "_flash_bench_files" not in src, fn.__name__
    assert not hasattr(ota_cycle, "_flash_bench_files"), "the /flash writer must be gone"
    assert not hasattr(ota_cycle, "_msc_put"), "its USB-MSC path goes with it"


# --- the RT1060's no-CDC route into its resident SBL ----------------------------------------

def _cli(*lines, rc=0, gap=0.3):
    """A real short-lived process that prints ``lines`` (``gap`` s apart) and exits ``rc`` -- a
    stand-in for the openmv-ota CLI, so imx_kick_catch's reader thread runs for real."""
    code = "import time\n" + "".join("print(%r, flush=True); time.sleep(%r)\n" % (ln, gap)
                                     for ln in lines) + "raise SystemExit(%d)\n" % rc
    return [sys.executable, "-c", code]


def _kick_rig(monkeypatch, *, port=True):
    events = []
    monkeypatch.setattr(ota_cycle, "jlink_reset_pulse", lambda b: events.append("pulse"))
    monkeypatch.setattr(ota_cycle, "_imx_kick", lambda p: events.append("kick") or True)
    monkeypatch.setattr(ota_cycle.os.path, "exists", lambda p: port)
    monkeypatch.setattr(ota_cycle, "_IMX_KICK_EVERY_S", 0.05)
    monkeypatch.setattr(ota_cycle, "log", lambda m: None)
    return events


def test_imx_markers_match_the_cli():
    """The harness waits on the CLI's own words; two copies of a string drift silently."""
    from openmv_ota.flash import flash as fl
    assert ota_cycle._IMX_ARMED == fl.IMX_ARMED
    assert ota_cycle._IMX_CLAIMED == fl.IMX_CLAIMED


def test_imx_kick_catch_pulses_after_arming_and_stops_kicking_at_the_claim(monkeypatch):
    events = _kick_rig(monkeypatch)
    rc, out = ota_cycle.imx_kick_catch(
        "OPENMV_RT1060", _cli("spsdk warming", ota_cycle._IMX_ARMED + " (x) for up to 60 s",
                              ota_cycle._IMX_CLAIMED, "erase ... write ... reset", gap=0.4),
        timeout=30)
    assert rc == 0 and ota_cycle._IMX_CLAIMED in out
    assert events[0] == "pulse" and "kick" in events, events   # reset FIRST, then the kick
    n = events.count("kick")
    import time
    time.sleep(0.3)
    assert events.count("kick") == n, "no kick may land once the SBL is claimed (it is flashing)"


def test_imx_kick_catch_does_nothing_when_the_cli_never_arms(monkeypatch):
    events = _kick_rig(monkeypatch)
    rc, out = ota_cycle.imx_kick_catch("OPENMV_RT1060", _cli("error: blhost not found", rc=2),
                                       timeout=30)
    assert rc == 2 and "blhost not found" in out
    assert events == [], "no reset, no kick without an armed catcher to catch the SBL"


def test_imx_kick_catch_without_a_port_never_kicks_and_times_out(monkeypatch):
    events = _kick_rig(monkeypatch, port=False)
    rc, out = ota_cycle.imx_kick_catch(
        "OPENMV_RT1060", _cli(ota_cycle._IMX_ARMED, gap=30), timeout=1.5, pulse=False)
    assert rc == 124 and "timed out" in out
    assert events == []


def test_imx_kick_types_stop_then_the_call_as_two_writes(monkeypatch):
    writes = []

    class FakeSerial:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def write(self, b):
            writes.append(b)

        def flush(self):
            pass
    import serial
    monkeypatch.setattr(serial, "Serial", FakeSerial)
    assert ota_cycle._imx_kick("/dev/ttyACM0") is True
    assert writes[0].count(b"\x03") >= 1 and b"bootloader" not in writes[0]
    assert writes[1] == b"import machine; machine.bootloader()\r"

    def gone(*a, **k):
        raise OSError(2, "No such file")
    monkeypatch.setattr(serial, "Serial", gone)
    assert ota_cycle._imx_kick("/dev/ttyACM0") is False


def test_rt1060_recovery_erase_is_the_romfs_via_the_kick(monkeypatch):
    """A bare i.MX `flash erase` wipes the /flash disk and leaves the app that breaks the CDC; and
    dfu_reset_catch's nRST cannot bring the RT1062's SBL up. Both were the old route."""
    seen = []
    monkeypatch.setattr(ota_cycle, "imx_kick_catch", lambda b, argv, **k: seen.append(argv) or (0, ""))

    def nope(*a, **k):
        raise AssertionError("the RT1060 has no DFU reset window")
    monkeypatch.setattr(ota_cycle, "dfu_reset_catch", nope)
    monkeypatch.setattr(ota_cycle, "log", lambda m: None)
    assert ota_cycle.recover_erase_romfs("OPENMV_RT1060") is True
    assert "--romfs" in seen[0] and "--in-bootloader" in seen[0] and "erase" in seen[0]


def _golden_rig(monkeypatch, *, cdc, cdc_rc=0, kick_rc=0):
    calls = []
    monkeypatch.setattr(ota_cycle, "_cdc_responsive", lambda *a, **k: cdc)
    monkeypatch.setattr(ota_cycle, "sh", lambda argv, **k: calls.append(("cdc", argv)) or (cdc_rc, "x\nerror: SBL"))
    monkeypatch.setattr(ota_cycle, "imx_kick_catch",
                        lambda b, argv, **k: calls.append(("kick", argv)) or (kick_rc, "boom"))
    monkeypatch.setattr(ota_cycle.time, "sleep", lambda s: None)
    monkeypatch.setattr(ota_cycle, "log", lambda m: None)
    return calls


def test_rt1060_golden_prefers_the_cdc_route(monkeypatch):
    calls = _golden_rig(monkeypatch, cdc=True)
    ota_cycle._flash_blhost_imx("OPENMV_RT1060")
    assert [c[0] for c in calls] == ["cdc"]
    assert "--in-bootloader" not in calls[0][1] and "factory" in calls[0][1]


def test_rt1060_golden_without_cdc_goes_in_bootloader_via_the_kick(monkeypatch):
    calls = _golden_rig(monkeypatch, cdc=False)
    ota_cycle._flash_blhost_imx("OPENMV_RT1060")
    assert [c[0] for c in calls] == ["kick"] and "--in-bootloader" in calls[0][1]


def test_rt1060_golden_falls_back_when_the_cdc_route_fails_and_raises_if_both_do(monkeypatch):
    import pytest
    calls = _golden_rig(monkeypatch, cdc=True, cdc_rc=2)
    ota_cycle._flash_blhost_imx("OPENMV_RT1060")
    assert [c[0] for c in calls] == ["cdc", "kick"]
    _golden_rig(monkeypatch, cdc=True, cdc_rc=2, kick_rc=2)
    with pytest.raises(RuntimeError, match="command failed"):
        ota_cycle._flash_blhost_imx("OPENMV_RT1060")

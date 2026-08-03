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
def test_ensure_cdc_waits_before_it_resets(board, monkeypatch):
    """Patient first, assertive after. Resetting on the FIRST attempt restarts a 33 s boot on every
    retry so none ever finishes; never resetting at all leaves a genuinely wedged board wedged, and
    the run dies before it flashes with "neither a DFU device nor a port". A core reset revives this
    board from no-USB-at-all in ~33 s, so it belongs on the later attempts."""
    monkeypatch.setattr(ota_cycle, "_cdc_responsive", lambda *a, **k: False)
    monkeypatch.setattr(ota_cycle, "_dfu_leave", lambda b: False)
    monkeypatch.setattr(ota_cycle, "sh", lambda *a, **k: (1, ""))
    order = []
    monkeypatch.setattr(ota_cycle, "_await_boot", lambda b, **k: order.append("wait") or False)
    monkeypatch.setattr(ota_cycle, "jlink_core_reset", lambda b, **k: order.append("reset"))
    monkeypatch.setattr(ota_cycle, "jlink_reset_pulse",
                        lambda *a, **k: pytest.fail("never the nRST pin on an Arduino"))
    ota_cycle._ensure_cdc(board)
    assert order[0] == "wait", "the first attempt must not reset a board that may just be booting"
    assert "reset" in order, "a wedged board must eventually get the reset that revives it"


@pytest.mark.parametrize("board", _ARDUINO)
def test_ensure_cdc_never_pulses_the_pin(board, monkeypatch):
    """The nRST PIN specifically: it leaves the core halted with no USB at all. (The debug-CORE
    reset is fine and necessary -- see test_ensure_cdc_waits_before_it_resets.)"""
    monkeypatch.setattr(ota_cycle, "_cdc_responsive", lambda *a, **k: False)
    monkeypatch.setattr(ota_cycle, "_dfu_leave", lambda b: False)   # not in DFU -> the wait path
    monkeypatch.setattr(ota_cycle, "sh", lambda *a, **k: (1, ""))
    waits = []
    monkeypatch.setattr(ota_cycle, "_await_boot", lambda b, **k: waits.append(b) or False)
    monkeypatch.setattr(ota_cycle, "jlink_core_reset", lambda *a, **k: None)
    monkeypatch.setattr(ota_cycle, "jlink_reset_pulse",
                        lambda *a, **k: pytest.fail("must not pulse the nRST pin on an Arduino"))
    ota_cycle._ensure_cdc(board)          # allow_erase defaults False -> returns after the retries
    assert waits, "expected the board to be given time to boot"


def test_swd_boards_reset_through_the_core_not_the_pin():
    """On a board WITH working SWD the pin must stay untouched -- a pulse whose follow-up connect
    fails leaves the core halted with no USB (measured on the Portenta, dark for minutes). The pin
    is only for boards that have no SWD to use, where nothing can halt the core anyway."""
    body = _body("jlink_core_reset")
    swd_branch = body.split('if BOARDS[board].get("jlink_swd", True):')[1].split("else:")[0]
    assert "connect" in swd_branch
    assert "SetRESET" not in swd_branch and "ClrRESET" not in swd_branch


def test_arduino_flash_watches_the_uart_and_does_not_probe():
    """The post-flash wait must be passive. An mpremote probe Ctrl-C's the app dead (KeyboardInterrupt
    is a BaseException the app's `except Exception` misses), which is what froze every boot at
    `data: path`."""
    body = _body("_flash_arduino_cli")
    assert "_await_boot" in body
    assert "_await_cdc" not in body, "the passive wait must replace the probe, not sit beside it"


def test_arduino_flash_only_watches_after_leave():
    """`:leave` needs NO help. Measured by running the harness's own flash command by hand and
    touching nothing: USB drops at :leave and the board re-enumerates 31 s later on its own, app
    checking in. An earlier version of this test asserted the opposite -- that belief came from runs
    where the probing that followed the flash was killing the app, and it cost a wasted cycle."""
    body = _body("_flash_arduino_cli")
    assert "_await_boot" in body
    # A pre-flash reset is fine (it is how a wedged board is reached at all). What must NOT happen
    # is resetting AFTER the write: :leave self-recovers, so a reset there is churn that restarts a
    # 33 s boot. Split on the flash call itself and check only what follows it.
    after_flash = body.split("_arduino_dfu_run")[-1]
    assert "jlink_core_reset" not in after_flash, ":leave self-recovers; no reset after the write"
    assert "_ensure_cdc" not in after_flash, (
        "nothing may probe the CDC after the flash -- that is what killed the app")


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


# ---------------------------------------------------------------------------
# Driving the board without the REPL: DFU to flash, UART to observe, J-Link to reset, USB-MSC for
# the bench files. The CDC probe is what killed the app, so the goal is to need the CDC nowhere.


def test_msc_write_is_idempotent(monkeypatch, tmp_path):
    """A host-side FAT write is invisible to the firmware until it re-mounts, so writing every run
    would mean a reset every run. Identical content must write NOTHING."""
    (tmp_path / ".hilcov_uart").write_bytes(b"1")
    monkeypatch.setattr(ota_cycle, "_msc_disk", lambda: "/dev/fake1")
    monkeypatch.setattr(ota_cycle, "sh", lambda *a, **k: (0, ""))
    assert ota_cycle._msc_put({".hilcov_uart": b"1"}, mnt=str(tmp_path)) is False


def test_msc_write_happens_when_content_differs(monkeypatch, tmp_path):
    (tmp_path / ".hilcov_uart").write_bytes(b"3")            # stale: a different UART
    monkeypatch.setattr(ota_cycle, "_msc_disk", lambda: "/dev/fake1")
    monkeypatch.setattr(ota_cycle, "sh", lambda *a, **k: (0, ""))
    assert ota_cycle._msc_put({".hilcov_uart": b"1"}, mnt=str(tmp_path)) is True
    assert (tmp_path / ".hilcov_uart").read_bytes() == b"1"


def test_msc_disk_refuses_to_guess_between_cameras(monkeypatch):
    """Two cameras on one node -> picking either is a coin flip that writes to the wrong board."""
    monkeypatch.setattr(ota_cycle.glob, "glob", lambda p: ["/dev/a-part1", "/dev/b-part1"])
    assert ota_cycle._msc_disk() is None


def test_bench_app_prints_its_device_id():
    """So the harness never takes the REPL to learn it."""
    assert "app: device_id" in ota_cycle.bench_main_py("ARDUINO_PORTENTA_H7", "wifi")


def test_verify_from_uart_rejects_a_pre_flash_mount(monkeypatch):
    """A `boot: mounted` from BEFORE the flash would verify the image the flash just replaced."""
    class Cap:
        raw = ["INFO openmv_ota: boot: mounted FRONT (payload 16777216)",
               "INFO openmv_ota: app: device_id ABC123"]      # both STALE, pre-flash
    monkeypatch.setattr(ota_cycle, "_CAP", Cap())
    monkeypatch.setattr(ota_cycle, "_FLASH_MARK", 2)          # the flash came AFTER those lines
    monkeypatch.setattr(ota_cycle.time, "sleep", lambda s: None)
    monkeypatch.setattr(ota_cycle, "verify_golden", lambda: "FELL-BACK")
    assert ota_cycle.verify_golden_uart("ARDUINO_PORTENTA_H7", budget=0) == "FELL-BACK"


def test_verify_from_uart_accepts_the_boot_the_flash_caused(monkeypatch):
    """The boot being verified happens between the flash and this call -- keying "fresh" off the
    CALL would demand a second boot nothing triggers, failing a board whose golden came up fine."""
    class Cap:
        raw = ["INFO openmv_ota: boot: mounted FRONT (payload 16777216)",
               "INFO openmv_ota: app: device_id ABC123"]
    monkeypatch.setattr(ota_cycle, "_CAP", Cap())
    monkeypatch.setattr(ota_cycle, "_FLASH_MARK", 0)          # flash preceded both lines
    monkeypatch.setattr(ota_cycle.time, "sleep", lambda s: None)
    assert ota_cycle.verify_golden_uart("ARDUINO_PORTENTA_H7", budget=30) == "ABC123"


def test_verify_from_uart_returns_the_id(monkeypatch):
    cap = type("C", (), {"raw": []})()
    monkeypatch.setattr(ota_cycle, "_CAP", cap)
    monkeypatch.setattr(ota_cycle, "_FLASH_MARK", 0)

    def boot(_s):
        cap.raw.append("INFO openmv_ota: boot: mounted FRONT (payload 16777216)")
        cap.raw.append("INFO openmv_ota: app: device_id DEADBEEF")
    monkeypatch.setattr(ota_cycle.time, "sleep", boot)
    assert ota_cycle.verify_golden_uart("ARDUINO_PORTENTA_H7", budget=30) == "DEADBEEF"


def test_verify_from_uart_never_execs():
    body = _body("verify_golden_uart")
    assert "device_exec" not in body, "the whole point is not to take the REPL"


def test_msc_disk_waits_for_the_board_to_enumerate(monkeypatch):
    """The disk only exists once the firmware is up (~33 s). Checking once and giving up falls back
    to the REPL -- and the REPL kills the app, which resets the board, which unenumerates the disk.
    Observed as a board rebooting once a second with app: CRASHED KeyboardInterrupt() every time."""
    calls = []

    def later(pattern):
        calls.append(pattern)
        return ["/dev/disk/by-id/usb-MicroPy_pyboard_Flash_X-part1"] if len(calls) > 3 else []
    monkeypatch.setattr(ota_cycle.glob, "glob", later)
    monkeypatch.setattr(ota_cycle.time, "sleep", lambda s: None)
    assert ota_cycle._msc_disk(budget=60).endswith("-part1")


def test_msc_disk_gives_up_eventually(monkeypatch):
    monkeypatch.setattr(ota_cycle.glob, "glob", lambda p: [])
    monkeypatch.setattr(ota_cycle.time, "sleep", lambda s: None)
    assert ota_cycle._msc_disk(budget=0) is None


def test_bench_app_stalls_after_a_crash():
    """A KeyboardInterrupt re-fires on restart, so die -> restart -> die spins as fast as the board
    boots: ~30 copies of the crash line inside ONE uart line, drowning the marker stream every
    scenario reads. The stall bounds it to one line every few seconds."""
    src = ota_cycle.bench_main_py("ARDUINO_PORTENTA_H7", "wifi")
    tail = src.split("app: CRASHED")[1]
    assert "sleep(5)" in tail, "the crash path must stall before letting the app restart"


@pytest.mark.parametrize("board", _ARDUINO)
def test_arduino_flash_takes_no_repl_to_reach_the_board(board, monkeypatch):
    """The flash needs a DFU device or an enumerated port -- never the REPL. Probing with mpremote
    Ctrl-C's the app dead, and that is what starts the crash spin."""
    body = _body("_flash_arduino_cli")
    assert "_ensure_cdc" not in body, "an mpremote probe here kills the app it is checking on"
    assert "_dfu_present" in body and "CFG[\"acm\"]" in body


# ---------------------------------------------------------------------------
# Completion for boards the update server never records.


@pytest.mark.parametrize("board", _ARDUINO)
def test_arduino_boards_are_marked_unrecorded(board):
    """The server's `unverified_boards` set skips the device-registry write for these, so a run that
    waits on device_record() can never conclude -- it watches until timeout while the device, still
    running, re-installs over and over (observed: repeated `image sha256 does not match the
    manifest` -> fallback -> re-offer, which looked like an OTA bug and was a harness deadlock)."""
    assert ota_cycle.BOARDS[board].get("server_record") is False


def test_recorded_boards_keep_the_server_check():
    """The four green boards ARE recorded; they must not be moved onto marker-only scoring."""
    for board, cfg in ota_cycle.BOARDS.items():
        if cfg.get("flash") != "arduino_cli":
            assert cfg.get("server_record", True) is True, board


def test_marker_scoring_requires_the_full_expect_set():
    """Marker scoring must not be a weaker gate -- `have` is `expect <= marks`, so a scenario still
    has to hit every path it declares."""
    body = _body("run_cycle")
    assert "by_marker" in body
    assert "reached = (have if by_marker else" in body


# ---------------------------------------------------------------------------
# The bench server's anti-rollback OFFER gate.


def test_offer_downgrades_is_off_by_default():
    """Relaxing the offer gate makes the server re-offer a release the device already installed.
    A device left running past its promotion is then told to install the same version forever:
    install -> confirm -> re-offer -> re-install -> `image sha256 does not match the manifest` ->
    fall back to golden -> re-offer. That looks like an OTA fault and is a bench misconfiguration."""
    import inspect

    import bench_server
    assert inspect.signature(bench_server.start).parameters["offer_downgrades"].default is False


def test_only_bad_version_relaxes_the_offer_gate():
    """bad_version exists to feed the device an offer a correct server would never make, so it is
    the one scenario that needs the gate down."""
    body = _SRC.split("srv = bench_server.start(")[1].split("\n\n")[0]
    assert 'offer_downgrades=(args.scenario == "bad_version")' in body


def test_env_omits_the_flag_when_not_requested(monkeypatch):
    """Off must mean ABSENT from the env, not present-and-false: the setting is read as a truthy
    string, so "0" would still arm it in some readings."""
    src = open(os.path.join(_CIHIL, "bench_server.py")).read()
    assert '**({"OPENMV_OTA_TEST_OFFER_DOWNGRADES": "1"} if offer_downgrades else {})' in src


# ---------------------------------------------------------------------------
# Reset on a board whose SWD pads are not wired.


def test_nicla_is_marked_reset_pin_only():
    """MEASURED on the bench: `connect` fails with "Could not connect to the target device" on the
    Nicla every time. The RESET pin is still wired, and driving it needs no target connection."""
    assert ota_cycle.BOARDS["ARDUINO_NICLA_VISION"].get("jlink_swd") is False
    assert ota_cycle.BOARDS["ARDUINO_PORTENTA_H7"].get("jlink_swd", True) is True


def test_reset_script_matches_the_board(monkeypatch, tmp_path):
    """A board with SWD gets connect+run; one without gets the pin ALONE. Sending `connect` to a
    board that cannot answer it wastes the whole timeout and reports a failure that is not one."""
    written = {}

    def fake_mkstemp(**kw):
        path = tmp_path / "s.jlink"
        return os.open(str(path), os.O_CREAT | os.O_WRONLY), str(path)
    monkeypatch.setattr(ota_cycle.tempfile, "mkstemp", fake_mkstemp)
    monkeypatch.setattr(ota_cycle, "sh",
                        lambda cmd, **kw: written.setdefault("script", open(cmd[-1]).read()) and None
                        or (0, ""))
    monkeypatch.setattr(os, "unlink", lambda p: None)

    ota_cycle.jlink_core_reset("ARDUINO_NICLA_VISION")
    assert "SetRESET" in written["script"] and "connect" not in written["script"]

    written.clear()
    ota_cycle.jlink_core_reset("ARDUINO_PORTENTA_H7")
    assert "connect" in written["script"] and "SetRESET" not in written["script"]


def test_verify_accepts_a_FALLBACK_mount(monkeypatch):
    """Golden reached by fallback logs a DIFFERENT line:

        boot: FRONT rejected (trial-failed) -> mounted BACK (payload ...)

    Matching only "boot: mounted" missed every one of those -- which is precisely what the negative
    scenarios produce -- and failed corrupt/bad_key/bad_version against boards that had booted fine."""
    class Cap:
        raw = ["WARNING openmv_ota: boot: FRONT rejected (trial-failed) -> mounted BACK (payload 16777216)",
               "INFO openmv_ota: app: device_id CAFE"]
    monkeypatch.setattr(ota_cycle, "_CAP", Cap())
    monkeypatch.setattr(ota_cycle, "_FLASH_MARK", 0)
    monkeypatch.setattr(ota_cycle.time, "sleep", lambda s: None)
    assert ota_cycle.verify_golden_uart("ARDUINO_PORTENTA_H7", budget=30) == "CAFE"


def test_mount_matcher_does_not_match_the_mounting_step():
    """`boot: slot mounting` is the step BEFORE the mount lands -- treating it as a mount would
    verify a board that has not finished booting."""
    assert ota_cycle._mounted("DEBUG openmv_ota: boot: slot mounting") is False


def test_verify_budget_clears_a_slow_boot():
    """bad_key's mount arrived at the very edge of a 180s budget, leaving no room for the device_id
    line that follows it. The budget must have headroom over a slow boot, not sit on top of it."""
    import inspect
    assert inspect.signature(ota_cycle.verify_golden_uart).parameters["budget"].default >= 300


def test_crash_stall_feeds_an_armed_watchdog():
    """The stall is a harness addition; with a watchdog ARMED it would itself provoke a bite, so a
    stray Ctrl-C would become a spurious reset and break the scenarios that test the watchdog.
    Feed on the app's own cadence: bound the log rate without changing what the watchdog sees."""
    src = ota_cycle.bench_main_py("OPENMV_N6", "lan")
    tail = src.split("app: CRASHED")[1]
    assert "openmv_wdt" in tail and "feed()" in tail
    assert "sleep(5)" in tail, "must still bound the rate when there is no watchdog to feed"

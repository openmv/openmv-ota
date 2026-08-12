"""verify_golden must catch the board running the PREVIOUS scenario's bench app.

Every scenario's golden is version 1.0.0, so the payload check cannot tell them apart. When a
golden flash silently does not take, the board keeps the old app and the run measures the wrong
thing until it times out. That is the N6 `watchdog_bite` flakiness: it always follows `watchdog`,
whose app differs only in whether it stops feeding, so the stale app looks entirely healthy on the
UART while wdt.bit/wdt.stop can never arrive.
"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ci" / "hil"))

import ota_cycle  # noqa: E402


def _cap(lines):
    ota_cycle._CAP = types.SimpleNamespace(raw=list(lines))
    ota_cycle._FLASH_MARK = 0


GOLDEN = "boot: mounted A (payload 16777216)"


def test_the_wrong_scenario_app_is_caught(monkeypatch):
    _cap([GOLDEN, "app: scenario watchdog", "app: device_id abc123"])
    with pytest.raises(RuntimeError, match="watchdog_bite"):
        ota_cycle.verify_golden_uart("OPENMV_N6", budget=1, want_app="watchdog_bite")


def test_the_right_app_passes():
    _cap([GOLDEN, "app: scenario watchdog_bite", "app: device_id abc123"])
    assert ota_cycle.verify_golden_uart(
        "OPENMV_N6", budget=5, want_app="watchdog_bite") == "abc123"


def test_no_expectation_means_no_check():
    """Callers that do not care (or older boards with no tag) must be unaffected."""
    _cap([GOLDEN, "app: scenario watchdog", "app: device_id abc123"])
    assert ota_cycle.verify_golden_uart("OPENMV_N6", budget=5) == "abc123"


def test_an_untagged_app_does_not_trip_it():
    """A board whose app predates the tag has no `app: scenario` line -- do not fail it blind."""
    _cap([GOLDEN, "app: device_id abc123"])
    assert ota_cycle.verify_golden_uart(
        "OPENMV_N6", budget=5, want_app="watchdog_bite") == "abc123"


def test_the_bench_app_emits_the_tag():
    """The guard is worthless if the app never says which scenario it is."""
    src = ota_cycle.bench_main_py("OPENMV_N6", "lan", "watchdog_bite")
    assert "app: scenario watchdog_bite" in src


def test_the_reinstall_scenario_starts_from_a_PROMOTED_board():
    """The only scenario whose second phase begins somewhere other than golden.

    Every other scenario starts on the factory image, so "what happens when an update fails AFTER
    you have already taken one" was unreachable -- the run ends when the first cycle settles. In
    the field that state is the normal one, and the only thing behind the promoted image is the
    FACTORY golden: a failed install there does not cost you the update, it costs you every update
    ever taken.
    """
    spec = ota_cycle.SCENARIOS["reinstall"]
    assert spec["end"] == "promoted", "phase 1 must actually promote, or phase 2 proves nothing"
    then = spec["then"]
    assert then["publish"] == "corrupt_sha", "phase 2 must fail the INTEGRITY gate, not the transport"
    assert then["end"] == "golden", "and must record that it falls back to the factory image"
    assert "install.reject_sha" in then["expect"], "the sha256 gate firing is the point"
    assert "confirm.promoted" in then["forbid"], "a bad image must never promote"


def test_reinstall_is_in_the_stable_boards_suite_and_dispatchable():
    """Useless if it never runs. It rides with the other negative paths on the stable boards."""
    for board in ("OPENMV_N6", "OPENMV_RT1060"):
        assert "reinstall" in ota_cycle.regression_scenarios(board, ota_cycle.BOARDS[board]["network"])
    wf = (Path(__file__).resolve().parents[2] / ".github/workflows/hil-ota.yml").read_text()
    assert "reinstall]" in wf, "must be selectable from workflow_dispatch"


def test_every_scenario_with_a_second_phase_declares_what_it_needs():
    """A `then` block is run by the same machinery as phase 1, so it must carry the same fields."""
    for name, spec in ota_cycle.SCENARIOS.items():
        then = spec.get("then")
        if then is None:
            continue
        for field in ("desc", "publish", "version", "end", "expect"):
            assert field in then, "%s.then is missing %s" % (name, field)


def test_reinstall_phase2_is_scored_on_markers_not_slot_size():
    """Phase 2 must not pass or fail on how big the board's flash slot is.

    Its assertion is that the integrity gate fires from a NON-golden start and the bad image never
    promotes -- which the marker set states exactly. Additionally waiting for the device to settle
    back on golden requires the retry-exhaust fallback, and that is slot-sized: each attempt writes
    the whole image before the sha check fails, so on the N6's 12 MiB slot three attempts run past
    the watch window. Measured: N6 lan came back `missing=- forbidden=- reached=False` -- every
    marker hit, nothing promoted, simply not settled in time -- while the RT1060 (4 MiB) passed.
    `corrupt_sha` documents the same trap and only dodges it because its device never leaves golden.
    """
    then = ota_cycle.SCENARIOS["reinstall"]["then"]
    assert then.get("by_marker") is True
    assert "install.reject_sha" in then["expect"], "the gate firing is still the point"
    assert "confirm.promoted" in then["forbid"], "and a bad image must still never promote"


def test_run_cycle_lets_a_caller_force_marker_scoring():
    """The board-config route (`server_record: False`) is not enough -- a single PHASE may need it
    on a board that is otherwise recorded server-side."""
    import inspect
    sig = inspect.signature(ota_cycle.run_cycle)
    assert "by_marker" in sig.parameters
    assert sig.parameters["by_marker"].default is False, "opt-in only; boards keep their behaviour"


def test_promoted_scoring_survives_an_install_faster_than_the_poll():
    """A promoted leg must not need to CATCH the device on golden.

    `saw_golden` was a polled observation of a transient state: the harness reads the server's
    device record every 15 s and had to see the device report golden before the install flipped it
    to target. That is a race against install speed, and the blank-skip erase lost it -- the
    RT1060's slot erase went 54 s -> 4 s, so a delta install fits inside one poll gap. Measured:
    RT1060 lan `delta` and `watchdog` went 362 s/PASS -> 606 s/FAIL on the run that made installs
    faster, both with every expected marker present and nothing forbidden.

    The markers are the stronger witness and are not racy: the capture is reset when the window
    opens, so a promoted scenario's install.start -> install.committed -> confirm.promoted can only
    have been produced inside it. A device already sitting on target cannot fake them.
    """
    import inspect
    body = inspect.getsource(ota_cycle.run_cycle)
    code = "\n".join(ln.split("#", 1)[0] for ln in body.splitlines())
    reached = code.split("reached = ")[1].split("return")[0]
    promoted = reached.split('end == "promoted"')[1].split("or (")[0]
    assert "saw_golden" not in promoted, "a fast install must not fail for being fast"
    assert "have" in promoted, "...but the in-window install markers must still be required"
    # the negative legs never install, so nothing shrinks their golden state -- they keep the check
    golden = reached.split('end == "golden"')[1]
    assert "saw_golden" in golden, "end=golden legs still assert the device settled back"


def test_phase2_publishes_only_after_the_window_reset_has_landed():
    """The bad update must not be offerable while the board is still running the promoted image.

    `run_cycle` opens every window with a hard reset. Publishing BEFORE that call leaves the update
    live during the seconds before the reset lands, and the device polls every ~5 s -- so it can
    start installing and then be reset MID-ERASE. Measured on RT1060 lan `reinstall` phase 2:
    offered at :38, erasing at :39, UART severed mid-word, board back 66 s later on
    `boot: rejected A:magic -> mounted B`. The phase then cannot pass -- the install never reached
    the sha check that `install.reject_sha` asserts, and the half-written slot leaves the device
    unsettled, so the server correctly refuses to offer again and the leg deadlocks.

    It is a RACE, so it must be closed by ORDERING, not by a delay: the identical code passed on
    the bench (reset :31, offer :31) and failed in CI (offer :38, reset :39).
    """
    import inspect
    src = inspect.getsource(ota_cycle.main)
    seg = src.split('then = spec.get("then")')[1]
    assert "after_reset=publish2" in seg, "phase 2 must hand its publish to run_cycle"
    # ...and must NOT publish eagerly at the call site
    call = seg.split("result2 = phase(")[0]
    assert "phase(\"publish2\"" in call and "publish2 = lambda" in call, "deferred, not removed"
    assert call.index("publish2 = lambda") < call.index("cap.reset"), "bound before the window opens"

    body = inspect.getsource(ota_cycle.run_cycle)
    code = "\n".join(ln.split("#", 1)[0] for ln in body.splitlines())
    assert code.index("after_reset()") > code.index("machine.reset()"), \
        "the hook must fire after the whole reset cascade, not before it"
    assert code.index("after_reset()") < code.index("deadline = "), "...and before the watch loop"


def test_no_slot_seeds_the_marker_uart_where_the_brick_cannot_erase_it():
    """`no_slot` destroys its own instrumentation unless the UART config is moved out of harm's way.

    `.hilcov_uart` is baked into the ROMFS (the one volume that survives an armed watchdog), and
    this scenario's brick erases the WHOLE romfs region -- so the erase that creates the condition
    under test also removes the file naming the marker UART. openmv_log falls back to its USB
    branch, and boot.py prints its markers BEFORE the CDC enumerates, i.e. into nothing. Measured on
    the RT1060: zero bytes on the UART *and* the console for 900 s from a healthy, REPL-answering
    board, failing on `boot.no_slot` -- a marker it certainly printed.

    Reading the console instead does not work (same enumeration reason) and actively harms: it
    holds the port the nudge-reset needs. /flash survives a romfs erase and openmv_log searches it,
    so the brick seeds it there first.
    """
    import inspect
    body = inspect.getsource(ota_cycle._flash_blhost_imx)
    seed = body.split("if bad_romfs:")[1].split("flash erase --romfs")[0]
    assert "/flash/.hilcov_uart" in seed, "the marker UART must be seeded somewhere the erase spares"
    assert "cov_uart" in seed, "...and it must name THIS board's UART"


def test_trace_write_creates_its_directory():
    """A finished scenario must not be lost to a missing directory.

    The trace is written at the very END, after provision + flash + install + confirm, so a
    non-existent parent turns a PASSING run into a non-zero exit with no RESULT line and no trace
    to explain it -- indistinguishable from a scenario failure. Measured on the Portenta: the board
    completed the entire OTA correctly and the run still failed, on a node that simply had no
    ~/hil-traces yet.
    """
    import inspect
    src = inspect.getsource(ota_cycle.main)
    tail = src.split('trace["elapsed_s"]')[1]
    assert "makedirs" in tail.split("json.dump")[0], "create the directory before writing the trace"

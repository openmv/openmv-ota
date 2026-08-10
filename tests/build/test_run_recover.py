"""Host tests for run()'s wedged-transport escalation (``recover`` / ``_recover``).

Why this exists: a network stack can reach a state where every socket call fails
identically forever -- measured on the ATWINC1500 as 39 consecutive ``OSError(22)``
EINVAL check-ins after a reset landed mid-transfer, which never cleared on its own.
Retrying is not a recovery strategy, so run() escalates to a caller-supplied hook that
re-initialises the interface.

The hook runs precisely when things are already broken, so the property that matters
most is that a hook which THROWS cannot take the OTA task down with it: ``_recover`` is
called from run()'s loop body AFTER its ``except`` has been left, so an escape here would
kill the loop and leave a device permanently un-updatable -- a worse failure than the
wedge it was trying to fix.

Scope note: the consecutive-failure COUNTING lives inline in ``run()``, which is
device-only (network + asyncio) and covered on hardware by the HIL watchdog scenario,
not here. What is host-testable -- and tested here -- is the hook contract itself.
"""

from __future__ import annotations

import asyncio

import pytest

from openmv_ota.build.device import openmv_ota as rt


class _Log:
    """Collects (level, message) so the HIL witness lines can be asserted."""

    def __init__(self):
        self.lines = []

    def warning(self, m):
        self.lines.append(("warning", m))

    def info(self, m):
        self.lines.append(("info", m))

    def debug(self, m):
        self.lines.append(("debug", m))

    def text(self):
        return " | ".join(m for _, m in self.lines)


@pytest.fixture
def log(monkeypatch):
    lg = _Log()
    monkeypatch.setattr(rt, "log", lg)
    return lg


def test_sync_hook_is_called_and_both_witnesses_logged(log):
    calls = []
    asyncio.run(rt._recover(lambda: calls.append("re-init")))
    assert calls == ["re-init"]
    # Both witnesses matter: the first says we noticed, the second says the hook RETURNED.
    # Without the second, a hook that hangs looks identical to one that worked.
    assert "run: recovering transport" in log.text()
    assert "run: transport recovered" in log.text()


def test_async_hook_is_awaited_not_merely_created(log):
    """The generated main.py passes its ``async def bring_up_network`` straight in, so
    calling the hook only BUILDS a coroutine -- the work happens in the await. If the
    await were skipped, this test's flag stays False and (on device) the network would
    never actually come back while the log still claimed it had."""
    done = []

    async def hook():
        await asyncio.sleep(0)
        done.append("re-init")

    asyncio.run(rt._recover(hook))
    assert done == ["re-init"]
    assert "run: transport recovered" in log.text()


def test_a_throwing_hook_never_escapes(log):
    """The whole point: recovery runs when the device is already broken."""

    def hook():
        raise OSError(22, "EINVAL")

    asyncio.run(rt._recover(hook))          # must NOT raise
    assert "run: recover failed" in log.text()
    assert "run: transport recovered" not in log.text()   # and must not claim success


def test_an_async_hook_that_throws_also_never_escapes(log):
    """The failure can just as easily surface from the await as from the call."""

    async def hook():
        await asyncio.sleep(0)
        raise OSError(22, "EINVAL")

    asyncio.run(rt._recover(hook))
    assert "run: recover failed" in log.text()
    assert "run: transport recovered" not in log.text()


def test_failure_log_is_bounded_to_one_repr(log):
    """RAM budget: the device must not buffer a traceback for an error that can repeat
    every poll for the life of the device."""

    def hook():
        raise OSError(22, "x" * 10_000)

    asyncio.run(rt._recover(hook))
    failed = [m for lvl, m in log.lines if m.startswith("run: recover failed")]
    assert len(failed) == 1
    # One repr of the exception -- no traceback, no accumulation across calls.
    assert "Traceback" not in failed[0]


def test_sync_hook_runs_under_relax_but_the_await_does_not(log, monkeypatch):
    """A NIC re-init is a long blocking C op (the WINC's own chip reset sleeps 300 ms),
    which outruns a 100 ms watchdog window -- so a SYNC hook must run under relax().

    An ASYNC hook must not: its await yields to asyncio, where the app's own feed loop
    runs, and holding relax() across an await disables the watchdog for as long as the
    app cares to take -- turning a safety net into a hole.
    """
    depth = {"now": 0, "seen_in_sync_hook": None, "seen_in_await": None}

    class _Relax:
        def __enter__(self):
            depth["now"] += 1
            return self

        def __exit__(self, *a):
            depth["now"] -= 1
            return False

    monkeypatch.setattr(rt, "_wdt_relax", lambda: _Relax())

    def sync_hook():
        depth["seen_in_sync_hook"] = depth["now"]

    asyncio.run(rt._recover(sync_hook))
    assert depth["seen_in_sync_hook"] == 1, "a blocking re-init must be ISR-fed"

    async def async_hook():
        depth["seen_in_await"] = depth["now"]

    asyncio.run(rt._recover(async_hook))
    assert depth["seen_in_await"] == 0, "relax() must not span an await"
    assert depth["now"] == 0, "relax() must be exited on every path"


def test_run_accepts_the_hook_and_defaults_to_the_old_behaviour():
    """``recover=None`` is the default, so an existing app's loop is unchanged."""
    import inspect

    sig = inspect.signature(rt.run)
    assert sig.parameters["recover"].default is None
    assert sig.parameters["recover_after"].default == 3


def test_generated_app_wires_its_own_bring_up_as_the_hook():
    """The SCAFFOLDED main.py must pass the hook -- the library default is None, so an
    unwired app silently keeps retrying a wedged stack forever. Re-using the SAME bring-up
    it booted with is what makes re-creating the NIC object (the thing that clears a WINC
    wedge) happen on the recovery path too.

    This used to assert against a standalone example file that `project new` did not ship,
    so it guaranteed nothing about the app a user actually receives -- and the generated
    one did NOT wire the hook. Pin the template itself."""
    from openmv_ota.project.project import _APP_MAIN_OTA

    assert "recover=bring_up_network" in _APP_MAIN_OTA
    # And the hook must be the bring-up that CONSTRUCTS the NIC, not one that reuses a handle.
    assert "network.WLAN(network.STA_IF)" in _APP_MAIN_OTA


# --- the escalation must fire on TRANSPORT faults only ---------------------------------
# Caught on hardware: the H7 Plus's bad_sig / bad_key / bad_version legs each drove a
# spurious `run: recovering transport`. The transport was perfectly healthy -- the update
# was legitimately REJECTED. Counting rejections means a device rebuilds its network every
# `recover_after` polls, forever, over a release that is never going to validate. On the
# WINC that rebuild is a full chip reset (winc_init -> nm_bsp_reset).

def _run_src():
    import inspect

    from openmv_ota.build.device import openmv_ota as rt
    return inspect.getsource(rt.run) + inspect.getsource(rt._poll_forever)


def test_only_a_failed_checkin_increments_the_streak():
    """The counter must live in the CHECK-IN's own except, not one wrapping the whole cycle."""
    src = _run_src()
    checkin_block = src.split("resp = _checkin(")[1]
    after = checkin_block.split("else:")[0]
    assert "fails += 1" in after, "the streak must be driven by the check-in failing"
    # ...and everything past a SUCCESSFUL check-in must not be able to reach it.
    post = checkin_block.split("else:")[1]
    assert "fails += 1" not in post, (
        "a rejected release must never look like a wedged network")
    assert "install(" in post, "the install path belongs after a successful check-in"


def test_a_successful_checkin_clears_the_streak():
    """Proof the transport works, whatever the release turns out to be."""
    post = _run_src().split("resp = _checkin(")[1].split("else:")[1]
    assert "fails = 0" in post.split("try:")[0], (
        "reaching the else branch means the link is fine; the streak must reset there")


# --- the OTA loop must never die permanently -------------------------------------------
# Measured on an N6 post-bite boot: `run: OTA LOOP DIED OSError(2,)` and the OTA path was
# gone for the rest of that boot. The loop's setup (CA resolve, status read) sits OUTSIDE
# its while, so one transient error there was fatal rather than something to retry -- and
# because MicroPython reports a dead task to the REPL, not our logger, it was invisible.

def test_run_restarts_the_loop_after_an_exception():
    """A device that stops being updatable is the worst outcome this project has; a
    transient error must cost one poll, not the rest of the device's life."""
    import inspect

    from openmv_ota.build.device import openmv_ota as rt

    src = inspect.getsource(rt.run)
    body = src.split('"""')[-1]                    # past the docstring
    assert "while True:" in body, "run() must re-enter the loop, not call it once"
    exc = body.split("except Exception")[1].split("except BaseException")[0]
    assert "sleep" in exc, "back off a poll before re-entering, don't spin"
    assert "raise" not in exc, "an ordinary exception must NOT end the loop"


def test_cancellation_is_recorded_but_still_propagates():
    """CancelledError/KeyboardInterrupt mean somebody is deliberately stopping us -- asyncio
    shutdown, or a probe taking the REPL. Restarting through those would fight the caller,
    but they must still be logged: on the bench a harness Ctrl-C was previously
    indistinguishable from a hang, which cost real debugging time."""
    import inspect

    from openmv_ota.build.device import openmv_ota as rt

    base = inspect.getsource(rt.run).split("except BaseException")[1]
    assert "log.error" in base, "a cancelled OTA loop must say so"
    assert "raise" in base, "cancellation must still propagate"

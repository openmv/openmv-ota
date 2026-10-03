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


def test_the_whole_hook_runs_under_relax_await_included(log, monkeypatch):
    """A NIC re-init is a long blocking C op (the WINC's own chip reset sleeps 300 ms),
    which outruns a 100 ms watchdog window -- so the hook must be ISR-fed.

    That includes an ASYNC hook's await: the scaffolded bring_up_network() constructs the
    NIC before its first await, so the blocking work happens INSIDE the await, where the
    app's feed loop cannot run. Leaving the await unfed reset-looped an armed H7 Plus on
    every transport recovery (reset_cause=3 right after `run: recovering transport`).
    relax() is bounded by RELAX_MAX_MS, so this cannot become an unfed hang.
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
    assert depth["seen_in_await"] == 1, "an async hook's blocking work runs in its await"
    assert depth["now"] == 0, "relax() must be exited on every path"

    async def failing_hook():
        raise OSError(5)

    asyncio.run(rt._recover(failing_hook))
    assert depth["now"] == 0, "relax() must be exited when the await raises too"


def test_run_accepts_the_hook_and_defaults_to_the_old_behaviour():
    """``recover=None`` is the default, so an existing app's loop is unchanged."""
    import inspect

    sig = inspect.signature(rt.run)
    assert sig.parameters["recover"].default is None
    assert sig.parameters["recover_after"].default == 5


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


# --- a failed check-in retries on a short backoff, not a whole poll ---------------------
# Measured on the RT1062 over LAN: the mimxrt eth driver seeds a static address, isconnected()
# goes True at once, DHCP swaps the address ~2 s later -- so the first check-in of EVERY boot
# failed with EHOSTUNREACH, and the device then sat dark for a full hour-long poll.

def test_backoff_doubles_from_ten_seconds_up_to_the_poll_cap():
    mid = 0.5                                       # r=0.5 is the un-jittered value
    assert [rt._backoff(n, 3600, mid) for n in range(1, 11)] == [
        10, 20, 40, 80, 160, 320, 640, 1280, 2560, 3600]
    assert rt._backoff(500, 3600, mid) == 3600      # a long outage stays AT the cap ...
    assert rt._backoff(10 ** 6, 3600, mid) == 3600  # ... and the shift is bounded, not a bignum
    # A short app interval caps it too: a device polling every 5 s never waits longer to retry.
    assert rt._backoff(1, 5, mid) == 5
    assert rt._backoff(3, 300, mid) == 40 and rt._backoff(6, 300, mid) == 300


def test_backoff_keeps_the_jitter():
    """A server outage fails a whole fleet at once; without jitter they all retry in lockstep."""
    assert rt._backoff(1, 3600, 0.0) == pytest.approx(8.5)
    assert rt._backoff(1, 3600, 0.999) == pytest.approx(11.5, abs=0.01)
    assert rt._backoff(20, 3600, 0.0) == pytest.approx(3060)


def test_the_jitter_draw_is_uniform_in_the_unit_interval():
    draws = [rt._rand() for _ in range(200)]
    assert all(0.0 <= d < 1.0 for d in draws)
    assert len(set(draws)) > 1


def test_default_recover_after_spans_minutes_not_hours():
    """With the backoff, recover_after failures arrive within minutes. Five of them span
    10+20+40+80 = 150 s: past a boot-time DHCP swap or an AP reboot (which heal by themselves),
    well short of the ~3 h three hourly polls used to take to rebuild a wedged stack."""
    import inspect
    n = inspect.signature(rt.run).parameters["recover_after"].default
    span = sum(rt._backoff(k, 3600, 0.5) for k in range(1, n))
    assert 120 <= span <= 300


def test_a_failed_checkin_waits_the_backoff_and_success_resets_it():
    src = _run_src()
    checkin_block = src.split("resp = _checkin(")[1]
    failed, ok = checkin_block.split("else:")[0], checkin_block.split("else:")[1]
    assert "wait = _backoff(misses" in failed, "a transport failure must not wait a whole poll"
    assert "misses += 1" in failed
    assert "misses = 0" in ok.split("try:")[0], "a check-in that got through resets the backoff"
    # A recover is NOT proof the link is back: it must not reset the backoff.
    recover_branch = failed.split("fails >= recover_after")[1]
    assert "misses = 0" not in recover_branch


# --- the check-in interval: the app's own cadence, honoured; the server may only slow it ----

def test_an_app_interval_is_kept_against_the_servers_default_pacing():
    """The server's ordinary answer ALWAYS carries poll_after_s (3600 s on the hosted cloud).
    Honouring it would silently turn the app's CHECK_IN_S = 300 into an hour."""
    ordinary = {"update": False, "poll_after_s": 3600}
    assert rt._next_poll(ordinary, 300, 0.5) == 300
    assert rt._next_poll({"update": False, "poll_after_s": 5}, 300, 0.5) == 300
    assert rt._next_poll({"update": False}, 300, 0.5) == 300


def test_the_apps_interval_is_jittered():
    assert rt._next_poll({}, 300, 0.0) == pytest.approx(255)
    assert rt._next_poll({}, 300, 0.999) == pytest.approx(345, abs=0.1)


def test_a_throttled_answer_may_slow_the_device_but_never_speed_it_up():
    """Load-shedding: a 429 asks for longer, and an overloaded server must always get it."""
    assert rt._next_poll({"poll_after_s": 240, "throttled": True}, 30, 0.5) == 240
    assert rt._next_poll({"poll_after_s": 60, "throttled": True}, 300, 0.5) == 300
    assert rt._next_poll({"throttled": True}, 300, 0.5) == 300


def test_no_interval_leaves_the_device_server_paced():
    """run(poll_after_s=None): wait what the server said (it already jittered it)."""
    assert rt._next_poll({"poll_after_s": 3411}, None, 0.5) == 3411
    assert rt._next_poll({}, None, 0.5) == rt._POLL_DEFAULT_S == 3600


def test_run_defaults_to_server_paced_and_the_loop_uses_the_interval():
    import inspect
    assert inspect.signature(rt.run).parameters["poll_after_s"].default is None
    src = _run_src()
    assert "wait = _next_poll(resp, poll_after_s" in src
    assert "_backoff(misses, cap" in src and "cap = poll_after_s or _POLL_DEFAULT_S" in src

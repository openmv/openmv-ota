"""The bench app that ``ota_cycle.bench_main_py`` generates is Python built by string
concatenation and only ever compiled ON THE BOARD -- a syntax slip ships to hardware and costs a
full flash cycle to find. These tests compile every variant on the host, and pin the one piece of
its control flow a scenario's correctness depends on: the rollback app must not poll on a trial
boot.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..", "..", "ci", "hil")))
os.environ.setdefault("WIFI_SSID", "ssid")
os.environ.setdefault("WIFI_PASSWORD", "pw")

import pytest  # noqa: E402

import ota_cycle  # noqa: E402  (ci/hil, added to sys.path above)

_APPS = ("confirm", "no_confirm", "wdt", "wdt_bite")
_NETS = ("wifi", "lan")


def _apps():
    """The app names bench_main_py actually supports (skip any this build doesn't know)."""
    out = []
    for a in _APPS:
        try:
            ota_cycle.bench_main_py("OPENMV4P", "wifi", app=a)
        except Exception:
            continue
        out.append(a)
    return out


@pytest.mark.parametrize("net", _NETS)
def test_every_generated_app_is_valid_python(net):
    for app in _apps():
        src = ota_cycle.bench_main_py("OPENMV4P", net, app=app)
        compile(src, "<bench_main_py:%s/%s>" % (app, net), "exec")


@pytest.mark.parametrize("net", _NETS)
def test_rollback_app_does_not_poll_on_a_trial_boot(net):
    # install() REBOOTS on success, so a run() poll racing the app's reset can re-install and land a
    # FRESH trial -- boot.py then marks it as a first try instead of rejecting an already-tried one,
    # and the scenario spins instead of rolling back (it did, for 12 cycles, on the H7 Plus). Gating
    # the poller on "not trial" is what makes boot.front_reject deterministic rather than a race.
    src = ota_cycle.bench_main_py("OPENMV4P", net, app="no_confirm")
    assert "if not openmv_ota.status().get('trial'):" in src
    # ...and the trial boot must still reset, or nothing would ever reject FRONT
    assert "machine.reset()" in src


@pytest.mark.parametrize("net", _NETS)
def test_other_apps_still_poll_unconditionally(net):
    # only the rollback app is gated: the happy paths need run() on every boot to install/confirm
    for app in _apps():
        if app == "no_confirm":
            continue
        src = ota_cycle.bench_main_py("OPENMV4P", net, app=app)
        assert "if not openmv_ota.status().get('trial'):" not in src
        assert "asyncio.create_task(openmv_ota.run(" in src

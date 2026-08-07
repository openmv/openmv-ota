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


# --- the bench app must EXERCISE the wedge-recovery hook -------------------------------
# The H7 Plus's ATWINC1500 reaches a state where every check-in fails OSError(22) (EINVAL)
# forever -- the board alive and polling, never recovering. run() grew a `recover` hook for
# exactly that, and the generated main.py wires it... but the BENCH app did not, so the fleet
# ran the wedge every time and never once exercised the fix. A hook that ships untested is a
# hook we do not know works.

@pytest.mark.parametrize("board,net", [
    ("OPENMV4P", "wifi"),                 # WINC -- the board the wedge was measured on
    ("ARDUINO_NICLA_VISION", "wifi"),     # cyw43
    ("OPENMV_N6", "lan"),                 # LAN
])
def test_bench_app_passes_its_bring_up_as_the_recover_hook(board, net):
    for app in _apps():
        src = ota_cycle.bench_main_py(board, net, app=app)
        assert "async def _bring_up():" in src, "the bring-up must be a callable to be reusable"
        assert "await _bring_up()" in src, "boot must use the same definition"
        assert "recover=_bring_up" in src, (
            "run() must get the hook, or the fleet never exercises wedge recovery (%s/%s/%s)"
            % (board, net, app))


def test_winc_recover_reconstructs_the_nic():
    """On the WINC, re-CREATING the object is what clears the wedge: network.WINC() runs
    winc_init -> nm_bsp_reset, which drives EN/RST low and hard-resets the chip. A hook that
    reused an existing handle would just re-try a wedged chip forever."""
    src = ota_cycle.bench_main_py("OPENMV4P", "wifi", app="confirm")
    bring_up = src.split("async def _bring_up():")[1].split("async def main")[0]
    assert "network.WINC()" in bring_up, "recover must CONSTRUCT the NIC, not reuse a handle"


# --- the CA must live in the ROMFS, not on /flash ---------------------------------------
# A FAT filesystem is corruptible (a cancelled run has wedged the mimxrt's /flash before),
# and run() cannot even check in without the CA -- so putting it there made a corruptible
# filesystem a hard dependency for being updatable. Measured: after a deliberate watchdog
# bite the file was gone and run() died 161 times on ENOENT, never reaching a check-in.

def test_ca_is_read_from_the_romfs():
    """Baked into app/, so it ships inside every image the harness builds -- golden and
    update alike -- and is therefore present on any slot the board can actually boot."""
    assert ota_cycle.CFG["ca_board"].startswith("/rom/"), (
        "the CA must not depend on a filesystem a corruption or a bite can empty")


def test_the_generated_app_reads_the_ca_from_the_romfs():
    for net in _NETS:
        for app in _apps():
            src = ota_cycle.bench_main_py("OPENMV4P", net, app=app)
            assert "/rom/" in src and "/flash/bench-ca" not in src, (net, app)


def test_the_coverage_hint_is_not_tied_to_the_ca_location():
    """These moved apart deliberately: the .hilcov_uart path used to be derived from the CA's
    directory, so relocating the CA into the read-only romfs would have sent a /flash write
    into /rom. Losing the coverage hint costs visibility; losing the CA costs updatability."""
    import inspect

    src = inspect.getsource(ota_cycle._flash_bench_files)
    assert "bench_flash_dir" in src
    assert 'CFG["ca_board"].rsplit' not in src, "the hint must not follow the CA into the romfs"

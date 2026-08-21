"""The classic boards' file-transport legs: the generated bench app, the shared manifest
tamper, the probe parser, and the scenario/network pairing guard.

Like test_bench_app.py, everything here is host-checkable surface of code that otherwise only
ever runs on a bench node -- a slip ships to hardware and costs a flash cycle to find.
"""

import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..", "..", "ci", "hil")))
os.environ.setdefault("WIFI_SSID", "ssid")
os.environ.setdefault("WIFI_PASSWORD", "pw")

import pytest  # noqa: E402

import ota_cycle  # noqa: E402  (ci/hil, added to sys.path above)

from openmv_ota.ota import ES256, algorithm_for  # noqa: E402
from openmv_ota.ota.keys import generate_private_key  # noqa: E402
from openmv_ota.ota.manifest import (  # noqa: E402
    Manifest,
    pack_manifest,
    parse_manifest,
    signed_region,
)
from openmv_ota.ota.sign import sign_region, verify_region  # noqa: E402


# --- the generated bench app -----------------------------------------------------------------
def test_file_bench_app_is_valid_python():
    compile(ota_cycle.file_bench_main_py(), "<file_bench_main_py>", "exec")


def test_file_bench_app_confirms_only_a_trial_boot():
    # The app's ONE job: promote a fresh trial. A confirm on a non-trial boot would be a no-op at
    # best and mask a failed install at worst -- the probe's PASS requires the version to have
    # MOVED, so the confirm must be gated on the trial flag, not unconditional.
    src = ota_cycle.file_bench_main_py()
    assert "if st.get('trial'):" in src
    assert "openmv_ota.confirm()" in src
    # ...and no network anything: these builds have no TLS stack, and an import error at boot
    # would kill the app before the confirm.
    assert "network" not in src
    assert "run(" not in src


# --- the shared manifest tamper --------------------------------------------------------------
def _signed_manifest():
    alg = algorithm_for(ES256)
    priv = generate_private_key(alg)
    body = {"version": "1.1.0", "product_id": "p", "representations": []}
    m = Manifest(body=body, key_id=0x0100, sig_alg=ES256)
    m.signature = sign_region(priv, signed_region(m), alg)
    return bytearray(pack_manifest(m)), priv.public_key(), alg


def test_tamper_manifest_flips_the_signature_and_reseals_the_crc():
    # The whole point of the shared helper: the tampered manifest still PARSES (crc re-sealed,
    # key untouched) so the device reaches the signature verify -- and that verify fails.
    data, pub, alg = _signed_manifest()
    ota_cycle.tamper_manifest_bytes(data, "manifest")
    m = parse_manifest(bytes(data))                      # parse + crc still pass
    assert m.key_id == 0x0100                            # key lookup would still hit
    assert not verify_region(pub, signed_region(bytes(data)), m.signature, alg)


def test_tamper_manifest_key_flips_only_the_key_id():
    data, _, _ = _signed_manifest()
    ota_cycle.tamper_manifest_bytes(data, "manifest_key")
    m = parse_manifest(bytes(data))                      # parse + crc still pass
    assert m.key_id != 0x0100                            # ...but the trusted-key lookup misses


# --- the probe parser ------------------------------------------------------------------------
def test_file_probe_parses_the_last_probe_line(monkeypatch):
    monkeypatch.setattr(ota_cycle, "device_exec", lambda *a, **k: (
        0, "BENCH boot 16777216\nPROBE|16777472|True|False\n"))
    assert ota_cycle.file_probe() == (16777472, True, False)


def test_file_probe_reads_the_payload_version_not_a_version_key(monkeypatch):
    """status() has NO "version" key -- the encoded int lives in payload_version (the string is
    in identity()). Probing .get('version') would compare None forever and fail every healthy
    board, so pin both the probe's device code and the harness-side encoding."""
    sent = {}
    monkeypatch.setattr(ota_cycle, "device_exec", lambda code, **k: (
        sent.update(code=code), (0, "PROBE|16777216|True|False"))[1])
    ota_cycle.file_probe()
    assert "payload_version" in sent["code"]
    assert "get('version')" not in sent["code"]
    from openmv_ota.ota.version import encode_app_version
    assert ota_cycle._payload_version("1.1.0") == encode_app_version("1.1.0")


def test_file_probe_raises_after_retries_without_an_answer(monkeypatch):
    calls = []
    monkeypatch.setattr(ota_cycle, "device_exec", lambda *a, **k: (
        calls.append(1), "")[1] or (1, "no such device"))
    monkeypatch.setattr(ota_cycle.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="probe never answered"):
        ota_cycle.file_probe(retries=2)
    assert len(calls) == 2


# --- the scenario/network pairing guard ------------------------------------------------------
@pytest.mark.parametrize("board,scenario", [
    ("OPENMV2", "delta"),            # a file board cannot run a server scenario
    ("OPENMV_N6", "file_full"),      # a network board has no SD-staged artifact path
])
def test_main_refuses_a_scenario_transport_mismatch(board, scenario):
    p = subprocess.run(
        [sys.executable, os.path.join(_HERE, "..", "..", "ci", "hil", "ota_cycle.py"),
         "--board", board, "--scenario", scenario],
        capture_output=True, text=True,
        env={**os.environ, "WIFI_SSID": "s", "WIFI_PASSWORD": "p"}, timeout=60)
    assert p.returncode == 2                             # argparse error, not a fake board failure
    assert "does not run on network" in p.stderr


def test_list_regression_for_a_classic_board_is_the_file_list():
    p = subprocess.run(
        [sys.executable, os.path.join(_HERE, "..", "..", "ci", "hil", "ota_cycle.py"),
         "--board", "OPENMV3", "--list-regression"],
        capture_output=True, text=True,
        env={**os.environ, "WIFI_SSID": "s", "WIFI_PASSWORD": "p"}, timeout=60)
    assert p.returncode == 0
    assert p.stdout.split() == ["file_full", "file_bad_sig"]


# --- device-vs-host traceback discrimination -------------------------------------------------
_HOST_TRACEBACK = """Traceback (most recent call last):
  File "/home/runner/.cache/openmv-ota-hil/venv/bin/mpremote", line 6, in <module>
    sys.exit(main())
  File ".../mpremote/commands.py", line 89, in do_disconnect
    state.transport.close()
  File ".../serial/serialposix.py", line 708, in _update_rts_state
    fcntl.ioctl(self.fd, TIOCMBIC, TIOCM_RTS_str)
OSError: [Errno 5] Input/output error
"""

_DEVICE_TRACEBACK = """Traceback (most recent call last):
  File "<stdin>", line 2, in <module>
OSError: manifest signature does not verify
"""


def test_host_teardown_traceback_is_not_a_device_raise():
    """A SUCCESSFUL install() reboots the board mid-exec; the CDC dies under mpremote and its
    teardown prints a HOST traceback (serialposix EIO). The M4's first fleet leg scored that
    flawless install as "install raised: OSError: EIO" -- the discrimination is the fix: device
    tracebacks carry <stdin> frames, host ones never do."""
    assert not ota_cycle._device_raised(_HOST_TRACEBACK)
    assert ota_cycle._device_raised(_DEVICE_TRACEBACK)
    assert not ota_cycle._device_raised("")


def test_frozen_installer_staging_is_not_flash_touching():
    """The M7's textbook pre-erase refusal scored erased=True because "install: staged" matched
    the frozen fallback's "install: staged frozen installer (exec would not fit)" -- a line that
    stages installer CODE before the vet runs. The boundary is the erase, so only the
    flash-touching lines count."""
    m7_refusal = (
        "INFO openmv_ota: install: staged frozen installer (exec would not fit)\n"
        "INFO openmv_ota: install: fetching manifest /sdcard/OPENMV3-manifest.bin\n"
        "WARNING openmv_ota: install: reject bad signature\n"
        "WARNING openmv_ota: install: rejected before erase (OSError('manifest signature does not verify',))\n"
    )
    assert not ota_cycle._install_touched_flash(m7_refusal)
    assert ota_cycle._install_touched_flash(m7_refusal + "INFO openmv_ota: install: erasing A (262144 bytes) t=1\n")
    assert ota_cycle._install_touched_flash("install: installed + armed; rebooting into the trial")

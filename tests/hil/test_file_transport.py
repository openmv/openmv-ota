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
        0, "BENCH boot 1.0.0\nPROBE|1.1.0|True|False\n"))
    assert ota_cycle.file_probe() == ("1.1.0", True, False)


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

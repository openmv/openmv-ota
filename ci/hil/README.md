# HIL OTA test catalog

Live-hardware tests for the OTA system: provision a golden board from the current
tree, drive an OTA **scenario**, and verify the device behaves — while capturing
which code paths ran off the board's side-channel UART. This is the gate no host
test can be: install/boot/confirm run on real flash, across real reboots, over the
real network. It runs on each board's self-hosted bench runner, driven by the
[`hil-ota`](../../.github/workflows/hil-ota.yml) workflow on every pull request.

Each piece documents itself — the docstrings and data-structure comments are the
reference, so they can't drift from the code:

- **`ota_cycle.py`** — one scenario run for one board. The module docstring has
  the env knobs and defaults; `BOARDS` holds the per-board facts, `SCENARIOS` the
  catalog (what each scenario drives and asserts, in its comments), `COVERAGE`
  the marker map, and `regression_scenarios()` what each board+network leg runs
  on a PR — and why the reduced suites are reduced.
- **`bench_server.py`** — the ephemeral per-run update server (plus the fake
  registrar); its docstring is the bench-topology story.
- **`hil_coverage.py`** — folds scenario traces into a device-path coverage
  report; its docstring explains how coverage works on a live, rebooting device.
- **`recover.py`** — brings a board back when its USB-CDC is gone, including the
  two-stage corrupt-firmware reflash; its docstring is the recovery manual.
- **`provision.sh`** — the runner-owned tooling bootstrap; its header comment and
  [NODE_REQUIREMENTS.md](NODE_REQUIREMENTS.md) split what the node image provides
  from what the workflow brings itself.
- **`run_matrix.sh`** — several scenarios back-to-back on a node, one trace each.

## Running

```sh
# one scenario
python3 ci/hil/ota_cycle.py --board OPENMV_N6 --network lan --scenario full --trace t.json

# a matrix on a node (traces -> ~/hil-traces/)
ci/hil/run_matrix.sh OPENMV_RT1060 lan corrupt rollback bad_sig delta

# coverage across every trace
python3 ci/hil/hil_coverage.py --traces ~/hil-traces --md cov.md --lcov cov.info

# recover a board whose USB-CDC is gone (see recover.py for --probe/--reset/--firmware)
python3 ci/hil/recover.py --board OPENMV4P
```

# HIL OTA test catalog

Live-hardware tests for the OTA system: provision a golden board from the current tree, drive
an OTA **scenario**, and verify the device behaves — while capturing which code paths ran off
the board's P4/P5 side-channel UART. This is the gate no host test can be: install/boot/confirm
run on real flash, across real reboots, over the real network.

It runs on each board's self-hosted bench runner, triggered by the
[`hil-ota`](../../.github/workflows/hil-ota.yml) workflow (`workflow_dispatch` only — not
per-commit while in development). Eventually this is the required gate for OTA-touching changes.

## Pieces

- **`ota_cycle.py`** — one scenario run for one board: provision → publish → hard-reset → watch
  the server record + the UART → PASS/FAIL + a JSON trace. Board flash/network are data-driven
  in `BOARDS`; the paths in `SCENARIOS`.
- **`run_matrix.sh`** — run several scenarios back-to-back on a node, one trace each into
  `~/hil-traces/`.
- **`hil_coverage.py`** — fold the traces into a **device-path coverage** report (markdown +
  lcov): which `# pragma: no cover` device lines the live hardware executed, and by which
  scenario.

## How coverage works on a live, rebooting device

Per-line coverage is impractical here: the cycle crosses reboots (install → `machine.reset()` →
trial → confirm), each a fresh MicroPython process, and the installer runs from RAM after
erasing its own slot. Instead **the OTA code's own log lines are the coverage** — `boot.py`, the
installer, and the runtime already log every path they take (`install: representation delta`,
`boot: FRONT rejected …`, `confirm: kept running FRONT`, …). `openmv_log` streams the whole log
at DEBUG to a hardware UART on **P4/P5** (`UART(3)` N6, `UART(1)` AE3/RT1062) when — and only
when — `/flash/.hilcov_uart` names a bus (the harness writes it; production boards lack it, so
the logger stays off and no UART opens). The bench reads that UART on the node's CP210x
`/dev/ttyUSB0`, so the trace survives every reboot and the DTR-reset of the USB-CDC console.

`COVERAGE` maps each stable log substring to a marker; `hil_coverage.py` maps each marker back
to the source `file:line` that emits it (longest-literal-prefix over the log call sites) and
aggregates across traces. So a renamed/dropped log line shows up as a missing marker — the
checklist and the code can't drift apart silently.

## Scenarios (`SCENARIOS`)

Each declares the markers it MUST hit (`expect`), must NOT (`forbid`), and an end state.
**PASS = reached the end state AND every expected marker fired AND none forbidden** — so a
dropped log line, a safety path that stopped running, or a wrong path firing all fail the run.

| scenario | how it's driven | asserts |
|---|---|---|
| `delta` | normal delta publish | install → trial → confirm → **promote** (delta path) |
| `full` | publish against an empty `--delta-from` | same, but the **full**-image path |
| `corrupt` | flip a byte in the published image blob | integrity fails → **retries** → **fallback** to golden BACK |
| `rollback` | bench app that never confirms + self-resets | trial boot → next boot **rejects FRONT** → golden BACK |
| `bad_sig` | flip a byte in the published manifest | signature fails → **refused pre-erase**, stays golden |
| `bad_version` | publish a version ≤ the floor (needs the server test hook) | version fails → **refused pre-erase**, stays golden |
| `no_slot` | erase BOTH romfs slots (no OTA) | boot finds **no bootable slot** (the brick floor) |

Together the scenarios cover **every** marker in `COVERAGE` — the full 16-point matrix.

Two scenarios need a bench-only assist:

- **`bad_version`** exercises the device's version anti-rollback, which a correct server won't
  let you reach: it refuses to OFFER a release `≤` a device's current version (and a device's
  floor is always `≤` current). The server's `test_offer_downgrades` setting
  (`OPENMV_OTA_TEST_OFFER_DOWNGRADES=1`) relaxes only that OFFER gate so the downgrade reaches
  the device — which still rejects it (the point). Safe by construction: it can't cause a
  rollback (the device is the boundary); the server logs a loud warning while it's on. Start the
  bench server with it set.
- **`no_slot`** bricks the board (erases both slots), so run it **after** another scenario (the
  board must be bootable — its firmware carries the bench logger and `/flash/.hilcov_uart` is
  set) and **reflash golden afterwards**. Block-device (RT1062) only for now.

## Bench topology note

`corrupt` and `bad_sig` **tamper the update server's artifact store**, so they only run where
the harness is **co-located with the server** (the RT1060 bench node). The others run on any
board. The write model is asserted per run: **XIP/ioctl** (N6, AE3) logs `install.xip`;
**block-device** (RT1062/mimxrt) logs `install.blockdev`.

## Bench requirements (per node)

- A pegged project at `~/proj` (`openmv-ota project new … --ota`), `~/ota-venv` (the tooling),
  `~/openmv-sdk-*`, plus the board's flasher: `~/jlink/JLinkExe` (N6), the SDK's `dfu-util`
  (AE3), or `blhost` via `machine.bootloader()` → resident SBL (RT1062).
- The board's USB-CDC at `/dev/ttyACM0` **and** its P4/P5 UART wired to a USB-serial bridge at
  `/dev/ttyUSB0`.
- The shared OTA server reachable (default `https://192.168.0.100:8443`) with `~/bench-ca.pem`
  on the node; the harness pushes it to `/flash/bench-ca.pem` on the board.

## Config (env / CI secrets)

`OTA_SERVER`, `OTA_TOKEN`, `OTA_CA_NODE`, `OTA_CA_BOARD`, `OTA_ARTIFACTS`, `WIFI_SSID`,
`WIFI_PASSWORD`, `PROJECT_DIR`, `OTA_VENV`, `SDK_HOME`, `JLINK`, `DFU_UTIL`, `BLHOST`,
`MPREMOTE`, `BOARD_ACM`, `BOARD_UART` — see the header of `ota_cycle.py` for defaults.

## Running

```sh
# one scenario
python3 ci/hil/ota_cycle.py --board OPENMV_N6 --network lan --scenario full --trace t.json

# a matrix on a node (traces -> ~/hil-traces/)
ci/hil/run_matrix.sh OPENMV_RT1060 lan corrupt rollback bad_sig delta

# coverage across every trace
python3 ci/hil/hil_coverage.py --traces ~/hil-traces --md cov.md --lcov cov.info
```

Validated on real hardware across all three OTA boards (N6/XIP, AE3/alif-XIP, RT1062/
block-device): the happy delta/full paths and the corrupt/rollback/bad_sig/bad_version/no_slot
safety paths — **all 16** device markers (the full matrix).

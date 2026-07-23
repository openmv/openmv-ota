# HIL OTA-cycle gate

`ota_cycle.py` is the live-hardware OTA test. It provisions a golden board from the
current tree, publishes an update, and verifies the device **installs → trials →
confirms → promotes** it fully autonomously — while capturing the code paths the run
actually executed as **HILCOV coverage markers** off the board's side-channel UART.

It runs on the board's self-hosted bench runner and is triggered by the
[`hil-ota`](../../.github/workflows/hil-ota.yml) workflow (`workflow_dispatch` only —
not per-commit while in development). Eventually this is the required gate for any
OTA-touching change.

## How coverage works on a live, rebooting device

Per-line coverage is impractical here: the cycle crosses reboots (install →
`machine.reset()` → trial → confirm), each a fresh MicroPython process, and the
installer runs from RAM after erasing its own slot. Instead the device emits **path
markers** — `openmv_hilcov.mark("install.delta")` writes `HILCOV install.delta` to a
hardware UART on **P4/P5** (`UART(3)` on the N6, `UART(1)` on the AE3). The bench reads
that UART on the node's CP210x `/dev/ttyUSB0`, so markers stream continuously across
every reboot — no dependency on the USB-CDC console (opening it DTR-resets the board)
or on the network flushing before a reset.

Markers are **opt-in**: they fire only when `/flash/.hilcov_uart` names a UART bus (the
harness writes it). Production boards lack that file, so the markers are fully inert and
no UART is opened.

The trace (`hil-trace-<board>.json`) records the pass/fail, per-phase timings, and the
set of markers hit. Building the full **path matrix** (delta vs full, retry, rollback,
block-device vs XIP, reject reasons …) and gating on "all required markers hit" is the
next step; today it records one passing `delta-happy` trace per board.

## Bench requirements (per node)

- A pegged project at `~/proj` (`openmv-ota project new … --ota`) and `~/ota-venv`
  (the tooling), `~/openmv-sdk-*`, plus the board's flasher: `~/jlink/JLinkExe` (N6) or
  the SDK's `dfu-util` (AE3).
- The board's USB-CDC at `/dev/ttyACM0` **and** its P4/P5 UART wired to a USB-serial
  bridge at `/dev/ttyUSB0`.
- The shared OTA server reachable (default `https://192.168.0.100:8443`), and the board
  on the bench network with `/flash/bench-ca.pem` present.

## Config (env / CI secrets)

`OTA_SERVER`, `OTA_TOKEN`, `OTA_CA_NODE`, `OTA_CA_BOARD`, `WIFI_SSID`, `WIFI_PASSWORD`,
`PROJECT_DIR`, `OTA_VENV`, `SDK_HOME`, `JLINK`, `DFU_UTIL`, `MPREMOTE`, `BOARD_ACM`,
`BOARD_UART` — see the header of `ota_cycle.py` for defaults. The workflow supplies the
server/token/WiFi from repo secrets/vars.

## Run it directly on a node

```sh
~/ota-venv/bin/python ci/hil/ota_cycle.py --board OPENMV_N6 --target 1.1.0
```

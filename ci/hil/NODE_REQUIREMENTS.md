# HIL node requirements — the infra / tooling split

The HIL nodes are **disposable infra**, built once from a maintained node image.
They carry **no OTA-specific hand-set-up state**: the OTA test workflow brings
everything it needs itself (`ci/hil/provision.sh` — its header comment lists
what, and where it caches), runner-owned under the runner's `$HOME`. A reimage
wipes that cache and the next run rebuilds it. This page is the *other* half of
the split: what the node image must provide, because an unprivileged runner
cannot set it up.

## The node image provides (root, set up once)

Nothing OTA-specific — only what *any* consuming repo needs on a self-hosted runner:

- **The runner** — the GitHub Actions runner registered as user `runner`, labelled
  `board-openmv-{n6,ae3,rt1062}` in the `hil` group.
- **Board access** — `runner` in `dialout` (for `/dev/ttyACM*` = the board USB-CDC, and
  `/dev/ttyUSB*` = the coverage-UART bridge) and `plugdev` (for `hidraw` = blhost/J-Link),
  plus generic `usb`/`hidraw` udev granting the group access (no per-VID rules needed).
- **git hygiene** — `git config --system --add safe.directory '*'`, so a build reading any
  checked-out tree isn't blocked by cross-owner "dubious ownership".
- **System packages** — `git`, `python3` (>=3.10) + `python3-venv`, `curl`, `tar`, `openssl`,
  and a host toolchain for the firmware build (`build-essential` = gcc/make + the usual headers).
  The cross-compiler + vela/signing tools come from the OpenMV SDK, which the workflow installs.

## Per-board hardware notes

- The board's USB-CDC and its coverage-UART bridge (a USB-serial adapter on the
  marker UART) must both be attached. **A missing or mis-wired bridge is the trap
  that silently costs a day**: every leg dies in 2 s on
  `could not open port /dev/ttyUSB0` and the board is not exercised at all, which
  reads as a failure rather than an absence.
- **Classic nodes** (`board-openmv-{m4,m7,h7}`): the board must have an **SD card
  inserted** — the file-transport legs stage the update artifacts on `/sdcard`
  over the USB-CDC. No marker UART and no J-Link are required on these nodes
  (their legs score the CDC probe).

## Applying to already-running nodes without a reimage

The image-level bits can be applied to a live node to *match* the image (never a
custom one-off). As root on the node:

```sh
usermod -aG dialout,plugdev,usb runner
git config --system --add safe.directory '*'
apt-get install -y git python3 python3-venv curl tar openssl build-essential
```

Then the OTA workflow self-provisions the rest on its next run.

## Adding a board to the bench

Three things have to line up, and a board is invisible to the gate until all
three do — each documented where it lives:

1. a **`BOARDS` entry** in `ci/hil/ota_cycle.py` (the per-board facts; the
   dict's comments explain each field);
2. a **leg in `.github/workflows/hil-ota.yml`** (`plan.legs`) with the runner
   `label`, plus a self-hosted runner registered with that label;
3. **`regression_scenarios()`** in `ota_cycle.py` — what that board+network
   runs, with the reasoning in its comments.

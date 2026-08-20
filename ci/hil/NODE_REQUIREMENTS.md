# HIL node requirements — the infra / tooling split

The HIL nodes are **disposable infra**, built once by [openmv-hil](https://github.com/openmv/openmv-hil).
They carry **no OTA-specific hand-set-up state**: the OTA test workflow brings everything it needs
itself (`ci/hil/provision.sh`), runner-owned and cached under the runner's `$HOME`. A reimage wipes
that cache and the next run rebuilds it. So the split is:

## openmv-hil provides (root / image, set up once)

Nothing OTA-specific — only what *any* consuming repo needs on a self-hosted runner:

- **The runner** — the GitHub Actions runner registered as user `runner`, labelled
  `board-openmv-{n6,ae3,rt1062}` in the `hil` group.
- **Board access** — `runner` in `dialout` (for `/dev/ttyACM*` = the board USB-CDC, and
  `/dev/ttyUSB*` = the P4/P5 coverage-UART bridge) and `plugdev` (for `hidraw` = blhost/J-Link),
  plus generic `usb`/`hidraw` udev granting the group access (no per-VID rules needed).
- **git hygiene** — `git config --system --add safe.directory '*'`, so a build reading any
  checked-out tree isn't blocked by cross-owner "dubious ownership".
- **System packages** — `git`, `python3` (>=3.10) + `python3-venv`, `curl`, `tar`, `openssl`,
  and a host toolchain for the firmware build (`build-essential` = gcc/make + the usual headers).
  The cross-compiler + vela/signing tools come from the OpenMV SDK, which the workflow installs.

## The workflow provides (runner-owned, per `ci/hil/provision.sh`)

Everything OTA-specific, under `$HIL_CACHE` (default `~/.cache/openmv-ota-hil`) + `$HOME`:

- a **venv** = `pip install -e <checkout>[server]` (pyserial + mpremote are core deps);
- the **firmware** = `git clone openmv` @ `OPENMV_REF` + shallow submodules;
- the **project** = `openmv-ota project new -f <fw> -b <board> --ota --dev --install-sdk`
  (the tool installs the **SDK** into `$HOME`; an AE3 project auto-scaffolds `app-coprocessor`);
- **J-Link** userspace (J-Link boards only; the SDK carries dfu-util + blhost for the rest).

## Per-board hardware notes

- **Classic nodes** (`board-openmv-{m4,m7,h7}`): the board must have an **SD card inserted** —
  the file-transport legs stage the update artifacts on `/sdcard` over the USB-CDC. No marker
  UART and no J-Link are required on these nodes (their legs score the CDC probe).

## Applying to already-running nodes without a reimage

The above image-level bits can be applied to a live node to *match* the image (never a custom
one-off). As root on the node:

```sh
usermod -aG dialout,plugdev,usb runner
git config --system --add safe.directory '*'
apt-get install -y git python3 python3-venv curl tar openssl build-essential
```

Then the OTA workflow self-provisions the rest on its next run.

## Adding a board to the bench

Three things have to line up, and a board is invisible to the gate until all three do.

1. **A `BOARDS` entry** in `ci/hil/ota_cycle.py`. Per-board facts, none of which can be guessed
   from the board name:
   - `cov_uart` — which UART the coverage markers come out of, wired to the node's
     `/dev/ttyUSB*` bridge. **This is the one that silently costs a day**: with no bridge (or the
     wrong UART) every leg dies in 2s on `could not open port /dev/ttyUSB0` and the board is not
     exercised at all, which reads as a failure rather than an absence.
   - `cov_write` — `install.xip` (stm32/alif/samd, memory-mapped romfs) or `install.blockdev`
     (mimxrt). This picks the write model, so getting it wrong fails everything.
   - `network` — the board's PRIMARY interface; a secondary one runs `delta` only.
   - `flash` — how golden is flashed: `dfu_cli` (OpenMV DFU), `blhost_imx` (mimxrt SBL), etc.
   - `jlink_device` — only for boards where SWD is used to reset a wedged DFU. Never to flash.
2. **A leg in `.github/workflows/hil-ota.yml`** (`plan.legs`) with the runner `label`, plus a
   self-hosted runner registered with that label.
3. **`regression_scenarios()`** — what that board+network runs. Boards differ: `no_slot` is
   block-device only, `coproc*` is AE3 only, and a **single-image board has no fallback slot**,
   so any scenario whose expectation is "falls back to the other slot" does not apply to it.

### Single-image boards specifically (M4 / M7 / H7 classic)

Their partition holds one image and its control sectors, so `derive_mode` returns `SINGLE` and
`boot.OtaBoot._slots()` yields one slot spanning the partition. The consequence that shapes the
scenario list: **a rejected image is terminal, not a fallback.** `boot.run()` raises
`OtaReject('no-slot:...')` and the device hands to firmware-resident recovery, which re-downloads
until something works. So `rollback` and anything else asserting `boot.fallback` is meaningless
there, while the negative paths that must still hold are the ones that refuse *before* erasing
(`bad_sig`, `bad_key`, `bad_version`) — on one slot, verify-before-flash is the entire safety
story. Both halves are emulated in `ci/qemu_boot_test.py` ("SINGLE mode: ..."), so the mode is
covered before any of these boards is on a bench.

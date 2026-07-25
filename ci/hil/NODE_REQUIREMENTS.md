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

## Applying to already-running nodes without a reimage

The above image-level bits can be applied to a live node to *match* the image (never a custom
one-off). As root on the node:

```sh
usermod -aG dialout,plugdev,usb runner
git config --system --add safe.directory '*'
apt-get install -y git python3 python3-venv curl tar openssl build-essential
```

Then the OTA workflow self-provisions the rest on its next run.

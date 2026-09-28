# CI

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs on every push /
PR; the workflow's own comment banners describe each job's intent
and setup subtleties. The gate that matters for device changes is
[`hil-ota.yml`](../.github/workflows/hil-ota.yml) — each board's full OTA
regression on the pull request, run on live hardware ([below](#the-hil-gate)).

- **`test`** — the host suite on Linux **and** macOS: `pytest` with the 100%
  coverage gate, including the pure logic of the device code (`boot.py`, the
  installer mirrors, the trial state machine — the test files document their
  own invariants).
- **`cshim`** — the ECDSA verify C shim (`device/ecdsa_verify.c`) compiled
  against the firmware's own mbedtls and exercised on the host: the host
  `cryptography` signs, the shim's mbedtls verifies, with gcov asserting 100%
  line coverage of the core.
- **`qemu`** — the **real** frozen `boot.py`, the installer, the runtime lib,
  and the manifest + delta paths on actual MicroPython under
  `qemu-system-arm`; [`qemu_boot_test.py`](qemu_boot_test.py) documents its
  scenarios. Run locally:
  `python ci/qemu_boot_test.py --firmware /path/to/openmv` (with the emulator
  boards built; `--board MPS3_AN547` restricts to one).
- **`build`** — every board built end to end through the *installed* CLI,
  exactly as a pip-installed user would, by the black-box driver
  [`build_boards.sh`](build_boards.sh): its header holds the per-class
  capability table (full / classic / noromfs), what each class must build,
  verify, or **refuse cleanly**, and the env toggles. The toolchain comes from
  the OpenMV SDK the tool itself installs — CI adds none.
- **`hil`** — a manual (`workflow_dispatch`) firmware build on a board's own
  runner; `hil-ota.yml` is the real hardware gate.
- **`postgres`** — the metadata store against a real Postgres 16 (a service
  container) with the real connection pool: SQL that only one dialect accepts
  and races between pooled connections, which the SQLite suite cannot see.
  Locally: `OPENMV_OTA_PG_DSN=<dsn> pytest -o addopts="" tests/server/test_metastore_postgres.py`
  (`pip install pgserver` gives a throwaway server with no install).
- **`gcp-kms`** — the GCP KMS signer against the real Cloud KMS service
  ([below](#the-gcp-kms-live-job)).
- **`premerged`** — on a push, whether the commit merges a PR whose CI already
  built this exact tree; if so `build` and `qemu` skip (the host tests always run).

## The GCP KMS live job

Signs through a real Cloud KMS key and checks the result with the device-side
verify ([`tests/ota/test_signer_gcp_live.py`](../tests/ota/test_signer_gcp_live.py)).
**No secret is stored in GitHub.** The job trades GitHub's OIDC token for
short-lived Google credentials (workload identity federation), so it needs
`id-token: write` and three **repository variables** (Settings → Secrets and
variables → Actions → **Variables**, not Secrets — they are identifiers):

| Variable | Value | What it is |
|---|---|---|
| `GCP_WIF_PROVIDER` | `<workload-identity-provider>` | the identity provider GitHub authenticates to |
| `GCP_CI_SERVICE_ACCOUNT` | `<ci-service-account>` | who the job acts as |
| `GCP_KMS_KEY_VERSION` | `<kms-key-version>` | the throwaway key it signs with |

The job does not run until `GCP_WIF_PROVIDER` is set, never runs for fork PRs
(GitHub issues them no OIDC token), and fails if the test skips instead of
passing.

What those point at, in GCP project `<project>` — all created for this job
and nothing else:

- **Key ring `<key-ring>`** (location `global`): the test key
  `<key>` (EC P-256, software, ~$0.06/month). Never a production
  key.
- **Service account `<ci-service-account>`**: holds only `roles/cloudkms.signerVerifier`
  on that one key — it can sign with it and nothing else.
- **Workload identity pool `github`, provider `openmv-ota`**: issuer
  `https://token.actions.githubusercontent.com`, attribute condition
  `assertion.repository == 'openmv/openmv-ota'`, so no other repository can use
  it; `roles/iam.workloadIdentityUser` on the service account is granted to
  `principalSet://…/workloadIdentityPools/github/attribute.repository/openmv/openmv-ota`.

To recreate it from scratch (as a project owner, with `gcloud` logged in):

```bash
P=<project>; N=<project-number>; SA=<ci-service-account>@$P.iam.gserviceaccount.com
gcloud services enable cloudkms.googleapis.com iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com --project $P
gcloud kms keyrings create <key-ring> --location global --project $P
# the key itself: `openmv-ota project keys backend provision --backend gcp-kms
#   --set key_ring=projects/$P/locations/global/keyRings/<key-ring>` on a throwaway project
gcloud iam service-accounts create <ci-service-account> --project $P
gcloud kms keys add-iam-policy-binding <key> --keyring <key-ring> \
  --location global --project $P --member serviceAccount:$SA --role roles/cloudkms.signerVerifier
gcloud iam workload-identity-pools create github --project $P --location global
gcloud iam workload-identity-pools providers create-oidc openmv-ota --project $P --location global \
  --workload-identity-pool github --issuer-uri https://token.actions.githubusercontent.com \
  --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition "assertion.repository == 'openmv/openmv-ota'"
gcloud iam service-accounts add-iam-policy-binding $SA --project $P --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/$N/locations/global/workloadIdentityPools/github/attribute.repository/openmv/openmv-ota"
```

## Running the board driver locally

```bash
pip install .                                                  # as a user would
ci/build_boards.sh /path/to/openmv OPENMV_N6 OPENMV4 ARDUINO_NANO_33_BLE_SENSE

# fast (skip the firmware compile); romfs/factory still build
NO_FIRMWARE=1 ci/build_boards.sh /path/to/openmv OPENMV_N6
```

Boards are positional (one or more); the env toggles (`NO_FIRMWARE`,
`INSTALL_SDK`, `WORKDIR`, `OPENMV_OTA_BIN`) are listed in the script header.
Exit code is 0 iff every check passed.

## The HIL gate

Live-hardware tests under [`hil/`](hil/): provision a golden board from the
current tree, drive an OTA **scenario**, and verify the device behaves — while
capturing which code paths ran off the board's side-channel UART. This is the
gate no host test can be: install/boot/confirm run on real flash, across real
reboots, over the real network, on each board's self-hosted bench runner.

Each piece documents itself — the docstrings and data-structure comments are the
reference, so they can't drift from the code:

- **`hil/ota_cycle.py`** — one scenario run for one board. The module docstring
  has the env knobs and defaults; `BOARDS` holds the per-board facts,
  `SCENARIOS` the catalog (what each scenario drives and asserts, in its
  comments), `COVERAGE` the marker map, and `regression_scenarios()` what each
  board+network leg runs on a PR — and why the reduced suites are reduced.
- **`hil/bench_server.py`** — the ephemeral per-run update server (plus the fake
  registrar); its docstring is the bench-topology story.
- **`hil/hil_coverage.py`** — folds scenario traces into a device-path coverage
  report; its docstring explains how coverage works on a live, rebooting device.
- **`hil/recover.py`** — brings a board back when its USB-CDC is gone, including
  the two-stage corrupt-firmware reflash; its docstring is the recovery manual.
- **`hil/provision.sh`** — the runner-owned tooling bootstrap; its header
  comment covers what it brings, against the node baseline below.
- **`hil/run_matrix.sh`** — several scenarios back-to-back on a node, one trace
  each.

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

## HIL node requirements — the infra / tooling split

The HIL nodes are **disposable infra**, built once from a maintained node image.
They carry **no OTA-specific hand-set-up state**: the workflow brings everything
it needs itself (`hil/provision.sh`), runner-owned and cached under the runner's
`$HOME` — a reimage wipes the cache and the next run rebuilds it. What follows
is the *other* half of the split: what the node image must provide, because an
unprivileged runner cannot set it up. Nothing OTA-specific — only what *any*
consuming repo needs on a self-hosted runner:

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

To bring an already-running node up to that baseline without a reimage (never a
custom one-off), as root:

```sh
usermod -aG dialout,plugdev,usb runner
git config --system --add safe.directory '*'
apt-get install -y git python3 python3-venv curl tar openssl build-essential
```

Then the workflow self-provisions the rest on its next run.

### Per-board hardware notes

- The board's USB-CDC and its coverage-UART bridge (a USB-serial adapter on the
  marker UART) must both be attached. **A missing or mis-wired bridge is the trap
  that silently costs a day**: every leg dies in 2 s on
  `could not open port /dev/ttyUSB0` and the board is not exercised at all, which
  reads as a failure rather than an absence.
- **Classic nodes** (`board-openmv-{m4,m7,h7}`): the board must have an **SD card
  inserted** — the file-transport legs stage the update artifacts on `/sdcard`
  over the USB-CDC. No marker UART and no J-Link are required on these nodes
  (their legs score the CDC probe).

### Adding a board to the bench

Three things have to line up, and a board is invisible to the gate until all
three do — each documented where it lives:

1. a **`BOARDS` entry** in `hil/ota_cycle.py` (the per-board facts; the dict's
   comments explain each field);
2. a **leg in `.github/workflows/hil-ota.yml`** (`plan.legs`) with the runner
   `label`, plus a self-hosted runner registered with that label;
3. **`regression_scenarios()`** in `hil/ota_cycle.py` — what that board+network
   runs, with the reasoning in its comments.

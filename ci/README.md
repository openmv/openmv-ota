# CI

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs on every push /
PR in five jobs; the workflow's own comment banners describe each job's intent
and setup subtleties. The gate that matters for device changes is
[`hil-ota.yml`](../.github/workflows/hil-ota.yml) — each board's full OTA
regression on the pull request, documented in [hil/README.md](hil/README.md).

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
  runner; `hil-ota.yml` above is the real hardware gate.

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

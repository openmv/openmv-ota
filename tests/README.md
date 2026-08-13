# Tests

Grouped by the subsystem under test, mirroring `src/openmv_ota/`. CI enforces **100%
coverage** (`--cov-fail-under=100`), so every line here is reachable by design — see
[../docs/reference/ci.md](../docs/reference/ci.md) for what runs where.

- `ota/` — the signed-artifact core: trailer codec, ECDSA algorithms + key handling,
  manifest codec + policy, the copy-with-difference delta codec, slot geometry, the
  status/rollback sectors.
- `build/` — the builders (romfs, provisioning image, firmware, manifests) **and** the
  device code they ship: `test_device_boot.py` is `boot.py`'s adversarial suite (malformed
  trailers, every status combination, replay/anti-rollback, the trial state machine),
  `test_installer.py` the installer's pure logic, `test_openmv_ota_runtime.py` the on-device
  SDK. `test_integration.py` is the black-box build → publish → install path.
- `project/`, `romfs/`, `flash/` — project pegging + config, low-level image packing, and
  the board flashing backends.
- `server/`, `client/` — the update server (check-in, rollouts, capability URLs, admin API,
  multi-tenancy) and the CLI that publishes to it.
- `hil/` — host-side **guards** for the hardware harness in `ci/hil/`: that every coverage
  marker still matches a live device log line, that every marker is expected by some
  scenario, and that no device-code line is unaccounted for. The harness itself runs on real
  boards, not here.

Two suites live outside pytest because they need something CI has to build: the **QEMU boot
test** (`ci/qemu_boot_test.py`, real MicroPython under `qemu-system-arm`) and the **ECDSA C
shim** compiled against the firmware's own mbedtls.

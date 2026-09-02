# Tests

Grouped by the subsystem under test, mirroring `src/openmv_ota/`. CI enforces
**100% coverage** (`--cov-fail-under=100`), so every line here is reachable by
design — [the CI page](../ci/README.md) says what runs where. The test files
document their own invariants; the map:

- `ota/` — the signed-artifact core: trailer, algorithms + keys, manifest,
  delta, slot geometry, status sectors.
- `build/` — the builders **and** the device code they ship: `boot.py`'s
  adversarial suite, the installer's pure logic, the on-device SDK, and the
  black-box build → publish → install integration test.
- `project/`, `romfs/`, `flash/` — project pegging + config, image packing,
  the flashing backends.
- `server/`, `client/` — the update server and the CLI that drives it.
- `hil/` — host-side **guards** for the hardware harness in `ci/hil/`, so the
  coverage markers, device log lines, and scenarios can't drift apart; the
  harness itself runs on real boards, not here.

Two suites live outside pytest because they need something CI has to build:
the QEMU boot test (real MicroPython under `qemu-system-arm`) and the ECDSA C
shim compiled against the firmware's own mbedtls — both on
[the CI page](../ci/README.md).

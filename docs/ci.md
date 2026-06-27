# CI

`.github/workflows/ci.yml` runs on every push / PR (and `workflow_dispatch`),
on **Linux and macOS**, in two jobs.

## `test` — unit tests + coverage

Installs the package with dev extras and runs `pytest`, which is configured
(`pyproject.toml`) to fail under **100% coverage**. Runs on `ubuntu-latest` and
`macos-latest`. This includes the device `boot.py` logic, which is pure and fully
host-tested (the hardware `_main` wiring is the only excluded part).

The **trial state machine** is covered exhaustively rather than by example, since its
edge cases are where update-safety bugs hide:

- `evaluate_slot` is tested against **all 8** `(pending, tried, confirmed)` marker
  combinations for **both** the FRONT slot (one-shot trial: arm → mount → roll back if
  un-confirmed; reject a `confirmed`-without-`tried` forgery) and the BACK slot (only the
  golden `(F,F,T)` mounts; every other state → `back-not-factory`).
- `OtaBoot.run` is tested for each *boot decision* — FRONT committed, FRONT on trial,
  FRONT `trial-failed` → roll back to golden BACK, FRONT signature-reject → BACK, a trial
  that can't be armed → BACK, and both slots failing → `no-slot`.
- `openmv_ota._should_confirm` is parametrized over slot × markers, pinning the slot
  guard: only a FRONT boot's un-confirmed trial confirms, so falling back to BACK never
  resurrects the failed FRONT image.

The **update path** is covered at every layer: the signed-manifest codec + policy
(`ota.manifest` — parse/verify/`update_reject_reason`/`select_representation`), the
copy-with-difference **delta codec** (`ota.delta` — make/apply across identical, scattered-
edit, shift, insert/delete, and truncation cases), and the device installer's *mirrors* of
both (pinned byte-for-byte to the host codecs). A **black-box end-to-end test**
([`tests/build/test_integration.py`](../tests/build/test_integration.py)) then publishes a
real golden→new image + delta + manifest with the build tools and consumes them through the
installer's own parse / select / streaming delta-apply, asserting the manifest's sha256+size
match the image and the install-time hash check passes — catching cross-tool drift.

## `cshim` — the ECDSA verify C shim

Compiles the shim's pure-C core (`device/ecdsa_verify.c`) against the firmware's
*own* mbedtls (3.6.2) and exercises it on the host: the host `cryptography`
(OpenSSL) signs and the shim's mbedtls verifies — proving the two agree — plus
tamper / wrong-key / wrong-length / unknown-alg / off-curve negatives, with `gcov`
asserting **100% line coverage of the core**. It fetches the mbedtls submodule chain
**recursively** — mbedtls 3.6 generates sources (e.g. `psa_crypto_driver_wrappers.h`)
via its own `framework` submodule — and installs mbedtls's own build requirements
(`scripts/basic.requirements.txt`, i.e. jinja2 + jsonschema) so `make libmbedcrypto.a`
can run; not the whole firmware, and crypto is OS-independent, so Linux is enough. A
separate test also compiles the shim with **no** mbedtls to prove the guard makes it
an empty unit (the AE3 M55_HE helper-core case). The MicroPython `mp_obj` glue is the
only untested part — that lands in the QEMU device test.

## `qemu` — boot.py on real MicroPython

Runs the **real** frozen `boot.py` on actual MicroPython under `qemu-system-arm`
on **two** machines — an MPS2-AN500 (Cortex-M7, a 4 MiB partition) and an
MPS3-AN547 (Cortex-M55, a 32 MiB partition) — covering what host unit tests can't:
that boot.py behaves the same on MicroPython, and that the real `vfs.rom_ioctl`
read + `vfs.VfsRom` mount + FRONT/BACK slot selection work on-device. The large
MPS3 partition specifically exercises the BACK slot **past the 16 MiB mark** — on
32-bit MicroPython a `memoryview`'s offset field is only 24-bit, so boot.py reads
each slot at its absolute XIP address via `uctypes.bytearray_at` rather than
slicing one whole-partition memoryview (which would overflow on the 24 MiB N6/AE3
partitions). [`ci/qemu_boot_test.py`](../ci/qemu_boot_test.py)
drives the device over the QEMU serial REPL via the firmware's bundled `mpremote`
(pasting a script — no filesystem mount) and checks six scenarios (the last folds in the
manifest + delta paths):

1. **All boot paths** — `evaluate_slot`/`parse_trailer` exercised for every reject
   reason (`magic`/`crc`/`key`/`sig`/`board`/`compat`/`size`/`body-sha`/`rollback`/
   `trial-failed`/`forged-confirm`/`status`/`back-not-factory`) and the valid cases,
   mirroring the host suite but on MicroPython.
2. **Real mount → FRONT** — a partitioned romfs (FRONT + BACK, distinct markers) is
   loaded into the emulated XIP region; `OtaBoot.run` reads it via `vfs.rom_ioctl`
   and mounts FRONT.
3. **Corrupt FRONT → BACK** — a broken FRONT body falls back to the golden BACK slot.
4. **Arm `tried` fails → BACK** — a pending FRONT with the *real* verified `write_marker`:
   the read-only qemu port rejects the write, so boot.py can't record the trial and falls
   back to the golden image (`reason trial-arm`) rather than running an untracked FRONT.
5. **`openmv_ota` runtime lib** — a romfs carrying the real `app/lib/openmv_ota/`
   runtime helpers + a matching `_ota_config` + a `/rom/system.json`, with the FRONT
   status sector crafted as an un-confirmed trial: `status()` reflects the slot (read via
   the `_ota_config` channel) + the trial, `identity()` reads system.json, `confirm()`
   keeps a FRONT trial but no-ops once we pretend we fell back to BACK (the slot guard),
   and `sync()` finds + plans its bundled resource. This covers the lib's device wiring
   (the read/decision/plan paths, `__file__`-based data resolution, the boot-result
   channel, the slot guard) that host tests can't reach. The flash *writes* no-op on the
   qemu port (read-only `rom_ioctl`), the same reason scenario 2's `write_marker` is
   stubbed; the writes use the same `rom_ioctl` API as `boot.py` and are covered by the
   host logic tests.
6. **`openmv_ota` installer** — the installer source (`data/installer.py`) is `exec`'d
   into RAM exactly as `install()` does on-device, then its logic is exercised on real
   MicroPython: `_parse_url`/`_is_blank`/`_chunk_size`, the `_Body` de-framing, the
   **`io.IOBase` + `deflate.DeflateIO` gzip-decompress chain** (a host-built gzip stream
   is decompressed on-device and compared), and the `_install_stream` erase/write/
   read-back/arm loop over a fake flash. This pins the one genuinely device-specific
   risk — that a pure-Python stream subclassing `io.IOBase` feeds `DeflateIO` correctly
   under MicroPython — which CPython host tests can't. It also checks the **`openmv_log`
   logger** on-device: it ships the real micropython-lib `logging.py` (the emulator
   boards don't freeze it; real OpenMV boards do), injects a fake `time` (the qemu port
   has no RTC), and drives a `logging.warning(...)` through the real logger + the custom
   `_OtaFormatter`, asserting the exact line `[2026-06-25 12:34:56] WARNING openmv_ota:
   qemu: live-log`. Finally it exercises the **manifest** path — parsing a real host-signed
   manifest under MicroPython (the struct/json/binascii/crc decode) + `_select_rep`/
   `_update_reject` — and the **delta** path end-to-end: a host-built gzipped patch streamed
   through the real `DeflateIO → _PatchReader → _delta_stream → _GenReader`, reconstructing
   the target against a stand-in golden and asserting the on-device **`ulab`** add ran
   (`_np is not None`) — ulab is built on every OTA-capable board (and the MPS emulators),
   so this is real vectorised reconstruction. The real `socket`/`ssl`/`rom_ioctl` wiring
   stays QEMU-unreachable (no network, read-only `rom_ioctl`) and is covered by host tests.

Both the image **and the manifest** signature checks use an injected/host-tested `verify`
because the qemu port doesn't build mbedtls yet (the ECDSA core is covered by `cshim`);
enabling mbedtls on the qemu port for real on-device crypto is a planned follow-up. The emulator boards don't
build mbedtls, so the tool refuses `project new --ota` for them (*not OTA-capable:
… build firmware without mbedtls*) — the job builds plain firmware (`project new` +
`build firmware`, no `--ota`) for both boards and needs `qemu-system-arm` +
`pyserial`/`platformdirs` (mpremote's deps). Run it locally with
`python ci/qemu_boot_test.py --firmware /path/to/openmv` (with both boards built;
add `--board MPS3_AN547` to restrict to one).

## `build` — every board, end to end

A matrix of **(os × board)** that builds each board's firmware / romfs /
factory-romfs and verifies the outputs. The whole point is that nobody can say
"it doesn't work" for some board or OS: every board is either built and verified,
or asserted to fail *cleanly* (a single structural error, never a traceback or a
wall of `make` output).

The per-board logic is a **black-box** bash driver,
[`ci/build_boards.sh`](../ci/build_boards.sh): it invokes only the installed
`openmv-ota` CLI (plus standard unix tools — `unzip`, `awk`, `wc`) exactly as a
pip-installed user would. Nothing in CI imports the Python package. Each board's
expected capability is a fixed table in the script (known board → known
behaviour), and the driver asserts the CLI's outcome:

| Class | Boards (examples) | What is asserted |
|---|---|---|
| **full** (OTA-capable) | N6, AE3, 4P, PT, RT1060, Portenta, Giga, Nicla | `project new --ota`; build firmware + romfs + factory-romfs; `inspect` + `verify` the OTA bundle (as a `.zip` and as loose `romfs.img`/`trailer.bin`) **and the factory image** (both FRONT + BACK slots); a corrupted body **and** a corrupted factory slot must **fail** verify; the factory image is the full partition. A multi-core board (AE3) also builds + checks its plain `coprocessor-romfs.img`. |
| **classic** (romfs, not OTA-capable) | OPENMV2 / 3 / 4 | `project new`; build firmware + single-image romfs; `project new --ota` must fail cleanly (*not OTA-capable*); `factory-romfs` must fail cleanly (*needs an OTA project*). |
| **noromfs** (no ROMFS partition) | Arduino Nano 33 BLE / RP2040 | `project new` must fail cleanly (*no partition size*). |

Every expected failure is asserted to be a clean tool error — non-zero exit, an
`error:` line, and **no Python traceback** — so a board the tool can't serve says
so structurally instead of exploding. Boards in the **noromfs** class never invoke
`make`: the tool refuses to create a project for them.

The factory image is crypto-verified too: `build inspect`/`build verify` understand
the dual-slot partition layout (they locate each slot's trailer by scanning
block-aligned offsets), so CI verifies **both** the FRONT and BACK slots through the
CLI and confirms a corrupted factory slot is rejected — no coupling to the tool's
internals, just the same `openmv-ota` a pip user runs.

### Toolchain — the SDK provides it

The whole point is to exercise the tool's own bootstrap, so the build jobs install
**no external toolchain**. They clone `openmv/openmv` (latest; `OPENMV_REF` at the
top of the workflow, default `master`) **with `--recursive` submodules** (the board
build needs micropython's nested submodules — lwip, mbedtls, mynewt-nimble,
cyw43-driver — not just openmv's direct ones), and `openmv-ota project new
--install-sdk` fetches the matching OpenMV SDK as a pure-Python download. The firmware build then
uses the compiler, `vela`, `stedgeai`, and the ST signing tool **bundled inside
that SDK** — the firmware Makefile prepends the SDK's `gcc/bin`, `python/bin`,
`stcubeprog/bin`, etc. to `PATH` automatically, so the only thing CI adds to `PATH`
is the SDK's own `make` (the top-level `make` the tool shells out to).

On **macOS** the build jobs also `brew install coreutils`: the firmware build calls
GNU `realpath`/`stat` (and the Alif port's `stat -c%s`), and macOS ships the BSD
variants that reject those flags. The SDK provides the compiler toolchain but not
GNU coreutils, so its `gnubin` is put on `PATH`.

> Set `OPENMV_REF` to a tag or 40-char SHA to pin the firmware for reproducibility;
> it defaults to `master` (latest).

## Running the board driver locally

```bash
pip install .                                                  # as a user would
ci/build_boards.sh /path/to/openmv OPENMV_N6 OPENMV4 ARDUINO_NANO_33_BLE_SENSE

# fast (skip the firmware compile); romfs/factory still build
NO_FIRMWARE=1 ci/build_boards.sh /path/to/openmv OPENMV_N6
```

Boards are positional arguments (one or more). Environment toggles:

| Var | Effect |
|---|---|
| `NO_FIRMWARE=1` | skip the slow firmware compile (romfs/factory still build, using the firmware tree's `mpy-cross` if present, else a pip-installed `mpy_cross`). |
| `INSTALL_SDK=1` | pass `--install-sdk` to `project new` (download the SDK if missing). |
| `WORKDIR=DIR` | where projects are created (default: a temp dir). |
| `OPENMV_OTA_BIN` | the CLI to invoke (default: `openmv-ota`). |

Exit code is 0 iff every check passed.

# CI

`.github/workflows/ci.yml` runs on every push / PR (and `workflow_dispatch`),
on **Linux and macOS**, in two jobs.

## `test` — unit tests + coverage

Installs the package with dev extras and runs `pytest`, which is configured
(`pyproject.toml`) to fail under **100% coverage**. Runs on `ubuntu-latest` and
`macos-latest`. This includes the device `boot.py` logic, which is pure and fully
host-tested (the hardware `_main` wiring is the only excluded part).

## `cshim` — the ECDSA verify C shim

Compiles the shim's pure-C core (`device/ecdsa_verify.c`) against the firmware's
*own* mbedtls (3.6.2) and exercises it on the host: the host `cryptography`
(OpenSSL) signs and the shim's mbedtls verifies — proving the two agree — plus
tamper / wrong-key / wrong-length / unknown-alg / off-curve negatives, with `gcov`
asserting **100% line coverage of the core**. It fetches only the mbedtls submodule
chain (not the whole firmware); crypto is OS-independent, so Linux is enough. The
MicroPython `mp_obj` glue is the only untested part — that lands in the QEMU
device test.

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
| **full** (OTA-capable) | N6, AE3, 4P, PT, RT1060, Portenta, Giga, Nicla | `project new --ota`; build firmware + romfs + factory-romfs; `inspect` + `verify` the OTA bundle (as a `.zip` and as loose `romfs.img`/`trailer.bin`); a corrupted body must **fail** verify; the factory image is the full partition. |
| **classic** (romfs, not OTA-capable) | OPENMV2 / 3 / 4 | `project new`; build firmware + single-image romfs; `project new --ota` must fail cleanly (*not OTA-capable*); `factory-romfs` must fail cleanly (*needs an OTA project*). |
| **noromfs** (no ROMFS partition) | Arduino Nano 33 BLE / RP2040 | `project new` must fail cleanly (*no partition size*). |

Every expected failure is asserted to be a clean tool error — non-zero exit, an
`error:` line, and **no Python traceback** — so a board the tool can't serve says
so structurally instead of exploding. Boards in the **noromfs** class never invoke
`make`: the tool refuses to create a project for them.

Crypto-verifying a *factory* image's two slots isn't done in CI: there is no CLI
for it (a pip user couldn't do it either), and reproducing the slot geometry in the
script would mean coupling CI to the tool's internals. The factory signing path is
covered instead by the OTA bundle's `verify` (same body, same signer) and the
100%-coverage unit tests; CI confirms the factory image builds and is the full
partition size.

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

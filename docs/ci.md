# CI

`.github/workflows/ci.yml` runs on every push / PR (and `workflow_dispatch`),
on **Linux and macOS**, in two jobs.

## `test` — unit tests + coverage

Installs the package with dev extras and runs `pytest`, which is configured
(`pyproject.toml`) to fail under **100% coverage**. Runs on `ubuntu-latest` and
`macos-latest`.

## `build` — every board, end to end

A matrix of **(os × board)** that builds each board's firmware / romfs /
factory-romfs and verifies the outputs. The whole point is that nobody can say
"it doesn't work" for some board or OS: every board is either built and verified,
or asserted to fail *cleanly* (a single structural error, never a traceback or a
wall of `make` output).

The per-board logic lives in [`ci/build_boards.py`](../ci/build_boards.py), which
derives each board's capability from the bundled board data + the OTA geometry
rules — the same source the tool uses — and asserts the expected outcome:

| Class | Boards (examples) | What is asserted |
|---|---|---|
| **full** (OTA-capable) | N6, AE3, 4P, PT, RT1060, Portenta, Giga, Nicla | `project new --ota`; build firmware + romfs + factory-romfs; `inspect` + `verify` the OTA bundle; a corrupted body must **fail** verify; both factory slots verify (FRONT confirmed-shape, BACK golden). |
| **classic** (romfs, not OTA-capable) | OPENMV2 / 3 / 4 | `project new`; build firmware + single-image romfs; `project new --ota` must fail cleanly (*not OTA-capable*); `factory-romfs` must fail cleanly (*needs an OTA project*). |
| **noromfs** (no ROMFS partition) | Arduino Nano 33 BLE / RP2040 | `project new` must fail cleanly (*no partition size*). |

Boards in the **noromfs** class never invoke `make` — the tool refuses to create
a project for them, structurally, so there is no firmware build to attempt.

### Toolchain — the SDK provides it

The whole point is to exercise the tool's own bootstrap, so the build jobs install
**no external toolchain**. They clone `openmv/openmv` (latest; `OPENMV_REF` at the
top of the workflow, default `master`), and `openmv-ota project new --install-sdk`
fetches the matching OpenMV SDK as a pure-Python download. The firmware build then
uses the compiler, `vela`, `stedgeai`, and the ST signing tool **bundled inside
that SDK** — the firmware Makefile prepends the SDK's `gcc/bin`, `python/bin`,
`stcubeprog/bin`, etc. to `PATH` automatically, so the only thing CI adds to `PATH`
is the SDK's own `make` (the top-level `make` the tool shells out to).

> Set `OPENMV_REF` to a tag or 40-char SHA to pin the firmware for reproducibility;
> it defaults to `master` (latest).

## Running the board driver locally

```bash
pip install -e ".[dev]"
python ci/build_boards.py --firmware /path/to/openmv          # all boards
python ci/build_boards.py --firmware /path/to/openmv \
    --boards OPENMV_N6 OPENMV4 --no-firmware                  # fast subset
```

`--no-firmware` skips the slow firmware compile (romfs/factory still build, using
the firmware tree's `mpy-cross` if present, else a pip-installed `mpy_cross`).
`--install-sdk` forwards to `project new` to download the SDK if it is missing.
Exit code is 0 iff every check passed.

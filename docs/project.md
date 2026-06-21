# project

`openmv-ota project` pegs an OTA project to a specific OpenMV firmware checkout
and records the toolchain versions and per-board geometry that firmware implies.
Model compilers (mpy-cross, Ethos-U Vela, ST Edge AI) must match the libraries
built into the firmware, so a project captures the exact versions to use.

A project is a directory you commit to git. It holds three files:

- `openmv-ota.toml` — the config you edit: product metadata and target boards.
- `openmv-ota.lock.json` — the resolved snapshot: the firmware's git remote and
  commit, submodule commits, firmware / MicroPython / SDK / tool versions, and
  per-board geometry. Committed, and contains no machine paths.
- `openmv-ota.local.toml` — this machine's firmware checkout path. Gitignored.

The firmware checkout itself is referenced by path, not copied into the project.

## Creating a project

`new` pegs a project to a local OpenMV checkout:

```bash
openmv-ota project new ./my-product -f ~/openmv -b OPENMV_N6 -b OPENMV_AE3
```

It reads the checkout and the installed SDK, writes the three files, and a
`.gitignore`. Commit `openmv-ota.toml` and `openmv-ota.lock.json`; the
`.gitignore` keeps `openmv-ota.local.toml` out of the repository.

`new` expects the OpenMV SDK to be installed already (run `make sdk` in the
firmware checkout). Pass `--install-sdk` to run `make sdk` for you, or
`--sdk-home PATH` to point at an SDK in a non-default location.

### Options

| Flag | Effect |
|---|---|
| `-f, --firmware PATH` | The local OpenMV checkout to peg to (required). |
| `-b, --board NAME` | A target board (repeatable, at least one). |
| `--product NAME` | Product name (defaults to the directory name). |
| `--vendor NAME` | Vendor name. |
| `--sdk-home PATH` | SDK install directory (default `~/openmv-sdk-<version>`). |
| `--install-sdk` | Run `make sdk` if the SDK is missing. |
| `--allow-dirty` | Don't warn when the checkout has uncommitted changes. |
| `--force` | Overwrite an existing project. |

## Reconstructing a checkout

When someone clones a committed project, they have the config and lock but not
the firmware. `setup` clones the pinned firmware and installs its SDK, then
writes their `openmv-ota.local.toml`:

```bash
openmv-ota project setup ./my-product
```

It clones the remote at the locked commit into a local cache (override with
`--cache PATH` or `$OPENMV_OTA_CACHE`), checks out the submodules, and runs
`make sdk`. Pass `--no-install-sdk` to skip the SDK build.

## Inspecting and updating

```bash
openmv-ota project show ./my-product          # the resolved snapshot
openmv-ota project show ./my-product --json   # the raw lock
openmv-ota project status ./my-product        # drift between lock and checkout
openmv-ota project verify ./my-product        # fail if anything has changed
openmv-ota project sync ./my-product          # re-resolve and rewrite the lock
```

`status` re-reads the current checkout and compares it to the lock, naming each
changed field. `sync` rewrites the lock from the current checkout when you intend
to move to a new firmware commit.

`status`, `verify`, and `sync` find the checkout from `openmv-ota.local.toml`, or
from `-f/--firmware`.

## Freezing the firmware

Once you build or release ROMFS images for a pegged firmware, the firmware must
not change — the images depend on the exact toolchain versions and board geometry
the project recorded. `verify` is the gate that enforces this:

```bash
openmv-ota project verify ./my-product
```

It exits 0 only when the checkout matches the lock in every recorded field **and**
the working tree is clean; otherwise it exits non-zero and lists what changed.
Uncommitted changes always fail, because the pinned commit does not capture them.
Run it in CI and before each image build.

Reading a project from Python verifies by default for the same reason — see below.

## What the lock records

`openmv-ota.toml` carries only what you choose (product metadata, target boards).
Everything else is resolved into `openmv-ota.lock.json`:

- the firmware version, git remote, commit, branch, `git describe`, and whether
  the checkout was dirty;
- the MicroPython version, its commit, and the `.mpy` ABI version;
- the SDK version, and the resolved mpy-cross, Vela, and ST Edge AI versions;
- every submodule commit;
- per board: the arch and mpy-cross flags, the NPU type and its full compiler
  config (Vela / ST Edge AI arguments and config-file references), the alignment
  rules, and the partition and FRONT sizes.

Partition sizes come from the firmware's `boards/<BOARD>/board_config.h`. When a
board's size is build-variant conditional, the bundled default is used instead
and the source is recorded in `geometry_source`; set `partition_size` under a
`[targets.<BOARD>]` table in `openmv-ota.toml` to override it.

## Reading a project from Python

`load_project` returns the lock plus this machine's resolved firmware path, SDK
home, and tool binary paths. It verifies that the checkout still matches the lock
(and is clean) first, raising if it does not, so a build never runs against a
changed firmware:

```python
from openmv_ota.project import load_project

p = load_project("./my-product")  # raises if the firmware has drifted
p.vela_path                       # path to the vela binary on this machine
p.board("OPENMV_N6").front_size   # firmware-resolved FRONT partition size
p.board("OPENMV_N6").alignment_rules
p.board("OPENMV_N6").npu_config   # NPU compiler type, args, and config-file refs
```

Pass `load_project("./my-product", verify=False)` to skip the check (reserved for
the firmware-update path, which does not yet exist).

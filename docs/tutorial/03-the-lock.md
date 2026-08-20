# The lock

*[← 2 · Projects](02-projects.md) · [Index](00-introduction.md) · [4 · OTA projects →](04-ota-projects.md)*

---

A project is only useful while its peg holds. This page is the lifecycle around
`openmv-ota.lock.json`: rebuilding a working machine from a committed project, watching
for drift, freezing the firmware once images ship, what the lock actually records, and
reading it all from Python.

## Reconstructing a checkout

When someone clones a committed project, they have the config and lock but not
the firmware. `setup` clones the pinned firmware and installs its SDK, then
writes their `openmv-ota.local.toml`:

```bash
openmv-ota project setup ./my-product
```

It clones the remote at the locked commit into a local cache (override with
`--cache PATH` or `$OPENMV_OTA_CACHE`), checks out the submodules, installs the
pinned SDK (the same pure-Python download + verify + extract as `--install-sdk`),
and pip-installs the matching mpy-cross (the firmware's MicroPython version) so
the machine is ready to build. Pass `--no-install-sdk` to skip the toolchain
steps and only clone.

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

```bash
openmv-ota project history ./my-product       # every recorded change to this project
openmv-ota project history ./my-product -n 20 # just the most recent 20
```

`history` replays the project's own event log — every resolve and re-sync, plus (for an
OTA project) its signing-key events — in order. It answers "when did this lock last move, and to what" without reading
`git log` of the checkout it points at.

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

Reading a project from Python verifies by default for the same reason.

## What the lock records

`openmv-ota.toml` carries only what you choose (product metadata, target boards,
and whether the project is OTA). Everything else is resolved into
`openmv-ota.lock.json`:

- whether the project is OTA (which halves each partition's usable image budget);
- the firmware version, git remote, commit, branch, `git describe`, and whether
  the checkout was dirty;
- the MicroPython version, its commit, and the `.mpy` ABI version;
- the SDK version, and the resolved mpy-cross, Vela, and ST Edge AI versions;
- every submodule commit, and each submodule’s remote (its upstream identity);
- per target (each board, and each of its targeted partitions): the arch and
  mpy-cross flags, the NPU type and its full compiler config (Vela / ST Edge AI
  arguments and config-file references), the alignment rules, and the partition
  size, flash erase block, and per-slot size.

Partition sizes come from the firmware's `boards/<BOARD>/board_config.h`. When
the size there depends on the build variant, the tool falls back to a bundled
default for the board. If either is wrong for your build, set `partition_size`
under a `[targets.<BOARD>]` table in `openmv-ota.toml` to override it. (On a
multi-core board the override applies only to the **main** partition; a
coprocessor partition always keeps its firmware geometry.)

The lock's `config_digest` covers only the *firmware-relevant* config — boards,
geometry overrides like `partition_size`, and the OTA mode — so changing any of
those is drift you must `sync`. Pure-identity fields (`product_id`, `board_name`)
and metadata (product / vendor name, app version) are deliberately **excluded**,
so editing a product id or bumping your app version never invalidates the lock.

## Reading a project from Python

`load_project` returns the lock plus this machine's resolved firmware path, SDK
home, and tool binary paths. It verifies that the checkout still matches the lock
(and is clean) first, raising if it does not, so a build never runs against a
changed firmware:

```python
from openmv_ota.project import load_project

p = load_project("./my-product")  # raises if the firmware has drifted
p.vela_path                       # path to the vela binary on this machine
p.targets                         # every (board, partition) target to build for
p.board("OPENMV_N6").front_size   # firmware-resolved per-slot size
p.board("OPENMV_N6").alignment_rules
p.board("OPENMV_AE3", 1).npu_config   # the second core's NPU type, args, and file refs
```

Pass `load_project("./my-product", verify=False)` to skip the check (reserved for
the firmware-update path, which does not yet exist).

---

*[← 2 · Projects](02-projects.md) · [Index](00-introduction.md) · [4 · OTA projects →](04-ota-projects.md)*

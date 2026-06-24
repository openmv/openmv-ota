[![GitHub Build](https://github.com/openmv/openmv-ota/actions/workflows/main.yml/badge.svg)](https://github.com/openmv/openmv-ota/actions/workflows/main.yml)
[![GitHub license](https://img.shields.io/github/license/openmv/openmv-ota?label=license%20%E2%9A%96)](https://github.com/openmv/openmv-ota/blob/master/LICENSE)
![GitHub release (latest SemVer)](https://img.shields.io/github/v/release/openmv/openmv-ota?sort=semver)
[![GitHub forks](https://img.shields.io/github/forks/openmv/openmv-ota?color=green)](https://github.com/openmv/openmv-ota/network)
[![GitHub stars](https://img.shields.io/github/stars/openmv/openmv-ota?color=yellow)](https://github.com/openmv/openmv-ota/stargazers)
[![GitHub issues](https://img.shields.io/github/issues/openmv/openmv-ota?color=orange)](https://github.com/openmv/openmv-ota/issues)

<img  width="480" src="https://raw.githubusercontent.com/openmv/openmv-media/master/logos/openmv-logo/logo.png">

# OpenMV OTA

Tooling for building OpenMV ROMFS images and delivering them to cameras over the
air. `openmv-ota romfs` builds the read-only `/rom` filesystem image; the
over-the-air update tools deliver signed, anti-rollback updates with a
golden-image fallback.

See [openmv-romfs-ota-concept-plan.md](openmv-romfs-ota-concept-plan.md) for the
OTA design.

- [Status](#status)
- [Installation](#installation)
- [Overview](#overview)
- [Contributing to the project](#contributing-to-the-project)
  + [Contribution guidelines](#contribution-guidelines)

## Status

The `openmv-ota romfs` image tool, `openmv-ota project` (firmware pegging), and
`openmv-ota build romfs` (app compile + ROMFS pack, with OTA trailer signing) are
implemented and tested. The remaining over-the-air update tools — slot
composition, the frozen `boot.py`, the on-device ECDSA verify module, and the
update server — are not yet built.

## Installation

> Not yet published. Once the package lands on PyPI, all tools install together:

```bash
pip install openmv-ota
```

For development, install from a checkout:

```bash
pip install -e .
```

## Overview

### ROMFS image tool

`openmv-ota romfs` packs a directory into an OpenMV ROMFS image and unpacks one
back. A ROMFS image is the read-only filesystem the camera mounts at `/rom`.

| Command | Purpose |
|---|---|
| `openmv-ota romfs pack <dir> -o <img> --board <board>` | Pack a directory into a ROMFS image (verbatim) |
| `openmv-ota romfs unpack <img> -o <dir>` | Unpack a ROMFS image to a directory |
| `openmv-ota romfs ls` / `cat` / `info` / `verify` | List, read a file from, summarise, or validate an image |
| `openmv-ota romfs boards` | List supported boards / show a board's ROMFS config |

```bash
openmv-ota romfs pack ./app -o app.romfs --board OPENMV_N6
openmv-ota romfs ls app.romfs -l
openmv-ota romfs unpack app.romfs -o ./out
```

`--board` sets the alignment rules and partition capacity for a camera;
`--align EXT=N` overrides the alignment for a file extension. See
[docs/romfs.md](docs/romfs.md).

### Project

`openmv-ota project` pegs an OTA project to a specific OpenMV firmware checkout
and records the toolchain versions and per-board geometry that firmware implies.
The project directory is committed and shared; build steps read it so their tool
versions match the firmware.

| Command | Purpose |
|---|---|
| `openmv-ota project new <dir> -f <openmv> -b <board>` | Create a project pegged to a firmware checkout |
| `openmv-ota project setup` | Reconstruct the pinned checkout and SDK from the lock |
| `openmv-ota project show` | Print the resolved snapshot |
| `openmv-ota project status` | Report drift between the lock and the checkout |
| `openmv-ota project verify` | Fail if the firmware has changed since it was pegged |
| `openmv-ota project sync` | Re-resolve and rewrite the lock |
| `openmv-ota project keys status/rotate/revoke` | Manage the OTA signing keys (OTA projects) |

```bash
openmv-ota project new ./my-product -f ~/openmv -b OPENMV_N6
openmv-ota project show ./my-product
```

Add `--ota` to `project new` to make it an over-the-air project: it splits each
partition into a runtime + golden image, provisions the signing keys, and
scaffolds the app, so `build romfs` can emit a signed image. See
[docs/project.md](docs/project.md).

`openmv-ota.toml` and `openmv-ota.lock.json` are committed and carry the firmware
identity, versions, and board geometry; `openmv-ota.local.toml` is gitignored and
holds this machine's checkout path.

### Build

`openmv-ota build romfs` compiles a project's app and packs a ROMFS image per
target — `.py` to `.mpy` with the pegged mpy-cross, and NPU models with the pegged
Vela / ST Edge AI. A non-OTA build writes `<board>.img`; an OTA build writes a
signed `<board>.zip` bundle (body + trailer + manifest). `build inspect` decodes a
bundle's trailer; `build verify` checks its signature + body hash against the
trusted keys (a CI / pre-publish gate).

```bash
openmv-ota build romfs   ./my-product
openmv-ota build inspect ./my-product/build/OPENMV_N6.zip
openmv-ota build verify  ./my-product/build/OPENMV_N6.zip
```

This is distinct from `romfs pack`, which packs a directory verbatim with no
compilation. See [docs/build.md](docs/build.md) and, for the signed image format,
[docs/trailer.md](docs/trailer.md).

### OTA

`project new --ota` and `build romfs` (above) already produce the signed,
anti-rollback image. The remaining pieces — slot composition, the frozen
`boot.py` and on-device ECDSA verify, and the update server — build on this; see
[openmv-romfs-ota-concept-plan.md](openmv-romfs-ota-concept-plan.md).

## Contributing to the project

Contributions are most welcome. If you are interested in contributing to the project, start by creating a fork of the repository:

* https://github.com/openmv/openmv-ota.git

Clone the forked repository, and add a remote to the main openmv-ota repository:
```bash
git clone https://github.com/<username>/openmv-ota.git
git -C openmv-ota remote add upstream https://github.com/openmv/openmv-ota.git
```

Now the repository is ready for pull requests. To send a pull request, create a new feature branch and push it to origin, and use Github to create the pull request from the forked repository to the upstream openmv/openmv-ota repository. For example:
```bash
git checkout -b <some_branch_name>
<commit changes>
git push origin -u <some_branch_name>
```

### Contribution guidelines
Please follow the [best practices](https://developers.google.com/blockly/guides/modify/contribute/write_a_good_pr) when sending pull requests upstream. In general, the pull request should:
* Fix one problem. Don't try to tackle multiple issues at once.
* Split the changes into logical groups using git commits.
* Pull request title should be less than 78 characters, and match this pattern:
  * `<scope>:<1 space><description><.>`
* Commit subject line should be less than 78 characters, and match this pattern:
  * `<scope>:<1 space><description><.>`

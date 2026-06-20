[![GitHub Build](https://github.com/openmv/openmv-ota/actions/workflows/main.yml/badge.svg)](https://github.com/openmv/openmv-ota/actions/workflows/main.yml)
[![GitHub license](https://img.shields.io/github/license/openmv/openmv-ota?label=license%20%E2%9A%96)](https://github.com/openmv/openmv-ota/blob/master/LICENSE)
![GitHub release (latest SemVer)](https://img.shields.io/github/v/release/openmv/openmv-ota?sort=semver)
[![GitHub forks](https://img.shields.io/github/forks/openmv/openmv-ota?color=green)](https://github.com/openmv/openmv-ota/network)
[![GitHub stars](https://img.shields.io/github/stars/openmv/openmv-ota?color=yellow)](https://github.com/openmv/openmv-ota/stargazers)
[![GitHub issues](https://img.shields.io/github/issues/openmv/openmv-ota?color=orange)](https://github.com/openmv/openmv-ota/issues)

<img  width="480" src="https://raw.githubusercontent.com/openmv/openmv-media/master/logos/openmv-logo/logo.png">

# OpenMV OTA

Secure over-the-air update tooling for OpenMV cameras.

The first subsystem is **ROMFS OTA**: a frozen `boot.py` plus host-side tooling
that delivers signed, anti-rollback ROMFS updates with a golden-image fallback —
MCUboot-grade defences against OTA-borne threats, implemented at the Python/ROMFS
level on top of `vfs.rom_ioctl`. The package is named broadly (`openmv-ota`) so
whole-firmware and bootloader OTA can be added as sibling subsystems later;
each subsystem is its own CLI command group (`openmv-ota romfs …`).

See [openmv-romfs-ota-concept-plan.md](openmv-romfs-ota-concept-plan.md) for the
full design — it is the source of truth for what this repo is building.

- [Status](#status)
- [Installation](#installation)
- [Overview](#overview)
- [Contributing to the project](#contributing-to-the-project)
  + [Contribution guidelines](#contribution-guidelines)

## Status

Early development, built out concept-by-concept against the plan document.

- **`openmv-ota romfs build` / `extract` — the core ROMFS image tool — is
  implemented and tested** (100% coverage). It packs a directory into an OpenMV
  ROMFS image with board-aware, per-extension alignment (so memory-mapped NPU
  model blobs land on the right boundary), and unpacks images back to a
  directory. The format is a faithful port of the OpenMV IDE's writer/reader and
  reproduces real IDE-built images byte-for-byte.
- The OTA layers (signing, the frozen `boot.py`, the `ed25519_verify` module,
  the update server) are still stubs. Model compilation (mpy-cross / Vela /
  ST Edge AI) is a planned layer on top of the core builder.

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

`openmv-ota` ships cooperating tools behind a single CLI, namespaced by
subsystem. The ROMFS image tool is available today:

| Command | Status | Purpose |
|---|---|---|
| `openmv-ota romfs build <dir> -o img --board B` | ✅ | Pack a directory into a ROMFS image (board-aware alignment) |
| `openmv-ota romfs extract <img> -o <dir>` | ✅ | Unpack a ROMFS image to a directory |
| `openmv-ota romfs ls` / `cat` / `info` / `verify` | ✅ | Inspect, read a file from, summarise, or validate an image |
| `openmv-ota romfs boards` | ✅ | List known boards / show a board's ROMFS config |
| `openmv-ota romfs build-firmware` | ▫ planned | Build openmv firmware with the frozen `boot.py` + `ed25519_verify` |
| `openmv-ota romfs pack` | ▫ planned | Compose a signed factory / OTA slot |
| `openmv-ota romfs serve` / `publish` | ▫ planned | Run the update server / upload a release |
| `openmv-ota init`, `keys generate` | ▫ planned | Scaffold a customer repo / create keys |

`--board` supplies the defaults (alignment rules + partition capacity); per-type
`--align EXT=N` flags override those on top. See [docs/romfs.md](docs/romfs.md).

```bash
openmv-ota romfs build ./app -o app.romfs --board OPENMV_N6
openmv-ota romfs ls app.romfs -l
openmv-ota romfs extract app.romfs -o ./out
```

The on-device SDK (`import openmv_ota` on the camera) is bundled as package data
and copied into the ROMFS at build time; customers never install it separately.

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

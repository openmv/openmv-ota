<p align="center">
  <img src="docs/under-construction.svg" width="100%" alt="Under Construction — this project is a work in progress">
</p>

[![CI](https://github.com/openmv/openmv-ota/actions/workflows/ci.yml/badge.svg)](https://github.com/openmv/openmv-ota/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/openmv/openmv-ota/graph/badge.svg?token=KNAA28U57K)](https://codecov.io/gh/openmv/openmv-ota)
[![GitHub license](https://img.shields.io/github/license/openmv/openmv-ota?label=license%20%E2%9A%96)](https://github.com/openmv/openmv-ota/blob/master/LICENSE)
![GitHub release (latest SemVer)](https://img.shields.io/github/v/release/openmv/openmv-ota?sort=semver)
[![GitHub forks](https://img.shields.io/github/forks/openmv/openmv-ota?color=green)](https://github.com/openmv/openmv-ota/network)
[![GitHub stars](https://img.shields.io/github/stars/openmv/openmv-ota?color=yellow)](https://github.com/openmv/openmv-ota/stargazers)
[![GitHub issues](https://img.shields.io/github/issues/openmv/openmv-ota?color=orange)](https://github.com/openmv/openmv-ota/issues)

<img  width="480" src="https://raw.githubusercontent.com/openmv/openmv-media/master/logos/openmv-logo/logo.png">

# OpenMV OTA

Secure over-the-air updates for OpenMV cameras: build your application into a
signed ROMFS image, publish it, and a camera downloads, verifies, and installs
it — falling back to the last release that worked if anything goes wrong.

**Start with the [tutorial](docs/tutorial/00-introduction.md)** — the complete,
navigable reference for every command and the update server's HTTP API, in the
order you use them: install → project → build → flash → device runtime → update
server. The command documentation lives there and only there, so it has one
place to be right.

Shipping in the EU? [docs/compliance/](docs/compliance/) maps this stack onto
the Cyber Resilience Act and RED 3.3 — what's covered, the
[residual threats](docs/compliance/residual-threats.md) that aren't — and
`project new --ota` scaffolds the fill-in templates (conformity checklist, EU
DoC, disclosure policy, security.txt) into your project's `compliance/`.

## Installation

> Not yet published. Once the package lands on PyPI, all tools install together:

```bash
pip install openmv-ota
```

For development, install from a checkout:

```bash
pip install -e .
```

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

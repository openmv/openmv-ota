<p align="center">
  <img src="docs/under-construction.svg" width="100%" alt="Under Construction — this project is a work in progress">
</p>

[![CI](https://github.com/openmv/openmv-ota/actions/workflows/ci.yml/badge.svg)](https://github.com/openmv/openmv-ota/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/openmv/openmv-ota/graph/badge.svg?token=KNAA28U57K)](https://codecov.io/gh/openmv/openmv-ota)
[![GitHub license](https://img.shields.io/github/license/openmv/openmv-ota?label=license%20%E2%9A%96)](https://github.com/openmv/openmv-ota/blob/main/LICENSE)
![GitHub release (latest SemVer)](https://img.shields.io/github/v/release/openmv/openmv-ota?sort=semver)
[![GitHub forks](https://img.shields.io/github/forks/openmv/openmv-ota?color=green)](https://github.com/openmv/openmv-ota/network)
[![GitHub stars](https://img.shields.io/github/stars/openmv/openmv-ota?color=yellow)](https://github.com/openmv/openmv-ota/stargazers)
[![GitHub issues](https://img.shields.io/github/issues/openmv/openmv-ota?color=orange)](https://github.com/openmv/openmv-ota/issues)

<img  width="480" src="https://raw.githubusercontent.com/openmv/openmv-media/master/logos/openmv-logo/logo.png">

# OpenMV OTA

Secure over-the-air updates for OpenMV cameras: build your application into a
signed ROMFS image, publish it, and a camera downloads, verifies, and installs
it — falling back to the last release that worked if anything goes wrong.

What a camera downloads is **encrypted**, under a key minted with the project and
baked into your own firmware, so a published release is ciphertext to the update
server, to the store it sits in, and to anyone who gets hold of it.

Every camera also has keys of its own, in a secure element on the boards that have one.
The desk that flashes a camera registers those keys to your account, which is how the
camera is claimed: from then on it signs its check-ins, and a release can be encrypted for
each camera alone.

**Start with the [tutorial](https://github.com/openmv/openmv-ota/blob/main/docs/tutorial/00-introduction.md)** — the complete,
navigable reference for every command and the update server's HTTP API, in the
order you use them: install → project → build → flash → device runtime → update
server. The command documentation lives there and only there, so it has one
place to be right.

Shipping in the EU? [The compliance mapping](https://github.com/openmv/openmv-ota/blob/main/docs/compliance/cra-red-alignment.md)
lays out how this stack lines up with the Cyber Resilience Act and RED 3.3 —
what's covered, and the [residual threats](https://github.com/openmv/openmv-ota/blob/main/docs/compliance/residual-threats.md)
that aren't — and `project new --ota` scaffolds the fill-in templates
(conformity checklist, EU DoC, disclosure policy, security.txt) into your
project's `compliance/`. Found a vulnerability in this stack itself? See
[the security policy](https://github.com/openmv/openmv-ota/blob/main/SECURITY.md).

## What each camera gets

What the stack does depends on the board: where its firmware and app live, whether the chip
can decipher flash as it reads it, and where it can keep a key.

**Updates.** A release always carries the app. On a board with a boot stub it carries the
firmware too, and two app slots mean a failed update falls back to the release that worked.

| Camera | Board | A release reaches | Slots | If an update fails |
|---|---|---|---|---|
| OpenMV N6 | `OPENMV_N6` | app + firmware | A/B, 12.0 MiB each | falls back to the other slot |
| OpenMV AE3 | `OPENMV_AE3` | app + firmware (both cores) | A/B, 12.0 MiB each | falls back to the other slot |
| OpenMV Cam RT1062 | `OPENMV_RT1060` | app + firmware | A/B, 4.0 MiB each | falls back to the other slot |
| OpenMV Cam H7 Plus | `OPENMV4P` | app + firmware | A/B, 4.0 MiB each | falls back to the other slot |
| OpenMV Pure Thermal | `OPENMVPT` | app + firmware | A/B, 4.0 MiB each | falls back to the other slot |
| Arduino Portenta H7 | `ARDUINO_PORTENTA_H7` | app + firmware | A/B, 2.0 MiB each | falls back to the other slot |
| Arduino Giga | `ARDUINO_GIGA` | app + firmware | A/B, 2.0 MiB each | falls back to the other slot |
| Arduino Nicla Vision | `ARDUINO_NICLA_VISION` | app + firmware | A/B, 2.0 MiB each | falls back to the other slot |
| OpenMV Cam H7 | `OPENMV4` | app (firmware over USB) | one, 120 KiB | downloads again |
| OpenMV Cam M7 | `OPENMV3` | app, over USB (firmware over USB) | one, 248 KiB | flashed again over USB |
| OpenMV Cam M4 | `OPENMV2` | app, over USB (firmware over USB) | one, 120 KiB | flashed again over USB |

**Secrecy.** What a camera downloads is encrypted on every board. What it then stores is
ciphertext where the chip can decipher flash as it is read, or has the RAM to hold a
deciphered copy, and the key to it never leaves the chip. A camera's own keys (its identity) are safest in a secure element.

| Camera | Download encrypted | Firmware in flash | App in flash | The flash key | The camera's own keys |
|---|---|---|---|---|---|
| OpenMV N6 | yes | encrypted | encrypted | derived in the chip from the hardware key ST fuses into it | in flash, sealed to the chip |
| OpenMV Cam RT1062 | yes | encrypted | encrypted | derived in the chip from fuses the camera burns and locks on its first boot | in a secure element (SE050) |
| OpenMV AE3 | yes | inside the chip (MRAM) | encrypted | made on the first boot, kept inside the chip | inside the chip, unsealed |
| OpenMV Cam H7 Plus | yes | in the clear, inside the chip | encrypted (read into RAM at boot) | made on the first boot, kept inside the chip | inside the chip, unsealed |
| OpenMV Pure Thermal | yes | in the clear, inside the chip | encrypted (read into RAM at boot) | made on the first boot, kept inside the chip | inside the chip, unsealed |
| Arduino Portenta H7 | yes | in the clear, inside the chip | in the clear | none | in a secure element (SE050) |
| Arduino Giga | yes | in the clear, inside the chip | in the clear | none | in a secure element (ATECC608) |
| Arduino Nicla Vision | yes | in the clear, inside the chip | in the clear | none | in a secure element (SE050) |
| OpenMV Cam H7 | yes | in the clear, inside the chip | in the clear, inside the chip | none | inside the chip, unsealed |
| OpenMV Cam M7 | yes | in the clear, inside the chip | in the clear, inside the chip | none | inside the chip, unsealed |
| OpenMV Cam M4 | yes | in the clear, inside the chip | in the clear, inside the chip | none | inside the chip, unsealed |

"Inside the chip" is the microcontroller's own flash or MRAM: nothing to desolder or probe,
but an open debug port reads it. No board locks its debug port yet; that, and secure boot,
are the device lockdown the
[residual threats](https://github.com/openmv/openmv-ota/blob/main/docs/compliance/residual-threats.md)
list as planned for the N6, the AE3 and the RT1062. The Arduino boards keep their app in the
clear: they have too little RAM to hold a deciphered copy.

**The hosted cloud.** Every camera can run against an update server you host. On the hosted
OpenMV Cloud:

| Camera | OTA updates | Console logs and telemetry | Live video |
|---|---|---|---|
| OpenMV N6, AE3, Cam RT1062, Cam H7 Plus, Cam H7 | yes | yes | yes (QVGA) |
| Arduino Portenta H7, Giga, Nicla Vision | yes | yes | yes (QVGA) |
| OpenMV Pure Thermal, Cam M7, Cam M4 | not offered | not offered | not offered |

## Installation

All tools install together, from [PyPI](https://pypi.org/project/openmv-ota/):

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

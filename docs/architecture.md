# Architecture

> Stub. The authoritative design is
> [../openmv-romfs-ota-concept-plan.md](../openmv-romfs-ota-concept-plan.md).
> This page will distill the implemented architecture as it is built.

## ROMFS OTA at a glance

- One ROMFS partition, virtually split 50/50 into **FRONT** (mutable runtime
  image) and **BACK** (immutable, factory-written golden image). The asymmetry of
  `vfs.rom_ioctl` erase — FRONT can be erased alone, BACK only by a full wipe —
  gives a golden image for free.
- Each slot: body + 0xFF padding + 4 KiB **status** sector + 4 KiB **trailer**.
- The trailer carries an ed25519 signature over a 128-byte signed prefix, a
  SHA-256 of the body, version/identity/provenance metadata, and a CRC32.
- A frozen, pure `boot.py` (ioctl + computation only) picks the slot: verify
  trailer → signature → body SHA → compatibility → anti-rollback → status state
  machine. On any FRONT failure it falls back to BACK.
- Host tooling (this package) builds firmware, composes + signs images, and
  serves updates. The on-device SDK drives trial-confirm, polling, and install.

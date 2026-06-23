# Architecture

> Stub. The authoritative design is
> [../openmv-romfs-ota-concept-plan.md](../openmv-romfs-ota-concept-plan.md).
> This page will distill the implemented architecture as it is built.

## ROMFS OTA at a glance

- One ROMFS partition, virtually split 50/50 into **FRONT** (mutable runtime
  image) and **BACK** (immutable, factory-written golden image). The asymmetry of
  `vfs.rom_ioctl` erase — FRONT can be erased alone, BACK only by a full wipe —
  gives a golden image for free.
- Each slot: body + 0xFF padding + a **status** sector + a **trailer**, each one
  flash erase block (4 KiB on OTA-capable boards). A board whose ROMFS is a single
  large internal-flash sector (OpenMV2/3/4) can't be split into slots and isn't
  OTA-capable.
- The trailer carries an ECDSA signature (COSE algorithm ids — ES256/P-256 by
  default, verified on-device by mbedtls) over a signed `header ‖ JSON-meta`
  region, a SHA-256 of the body, version/identity/provenance metadata, and a
  CRC32. See [trailer.md](trailer.md).
- A frozen, pure `boot.py` (ioctl + computation only) picks the slot: verify
  trailer → signature → body SHA → compatibility → anti-rollback → status state
  machine. On any FRONT failure it falls back to BACK.
- Host tooling (this package) builds firmware, composes + signs images, and
  serves updates. The on-device SDK drives trial-confirm, polling, and install.

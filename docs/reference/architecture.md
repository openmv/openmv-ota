# Architecture

> The design and its reasoning live in [v2-plan.md](v2-plan.md). This page is the
> short version: what is on the flash and who reads it.

## ROMFS OTA at a glance

- One ROMFS partition holding **two slots, A and B, of equal size**. Neither is
  privileged: both are real, signed, updatable images, and the one that boots is
  simply the newest valid one. An update is written into the slot the device is
  *not* running, so the image it is currently executing survives a failed install.
- **Single-image mode** exists for boards whose whole ROMFS is one erase sector
  (OpenMV2/3/4): one slot spanning the partition, no fallback. It is an explicit
  opt-out (`single_image = true`) everywhere else, because the cost — a failed
  update needs a network round trip, and a device that cannot reach the network
  needs a physical reflash — is invisible when you choose it and expensive later.
- Each slot: body + 0xFF padding + four control sectors (spare, **rollback**,
  **status**, **trailer**), 4 KiB each, at the end of the slot. Both modes use
  the same layout, so there is one on-flash shape rather than two.
- The trailer carries an ECDSA signature (COSE algorithm ids — ES256/P-256 by
  default, verified on-device by mbedtls) over a signed `header ‖ JSON-meta`
  region, a SHA-256 of the body, version/identity/provenance metadata, and a
  CRC32. See [trailer.md](trailer.md).
- The status sector carries the trial state machine (`pending` / `confirmed`),
  the **install counter** (`u32 ‖ ~u32`) that orders the two slots, and an
  **attempt region** — one byte consumed per trial boot, so a trial that *hangs*
  still makes progress toward giving up.
- A frozen, pure `boot.py` (ioctl + computation only) picks the slot: for each,
  verify trailer → signature → body SHA → compatibility → anti-rollback → trial
  state; then boot the survivor with the highest install counter. If none
  survives there is no factory image to retreat to — the device hands off to
  firmware-resident recovery, which re-downloads until it has a working image.
- Host tooling (this package) builds firmware, composes + signs images, and
  serves updates. The on-device SDK drives trial-confirm, polling, and install.

## The one invariant everything rests on

**One erase pass per slot, then writes only.** The installer erases its target
slot once and every write after that is a 1→0 program: the status markers as the
trial advances, the install counter, the rollback log appended to. Nothing is
ever rewritten in place, which is what lets a slot's control data share an erase
block with its body — and that, in turn, is what makes single-image mode possible
at all.

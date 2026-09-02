# Architecture

> The walkthrough of how you *use* all this is [the tutorial](../tutorial/00-introduction.md).
> This page is what is on the flash, who reads it, and the design decisions
> recorded so they are not re-derived.

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
- Each slot: body + 0xFF padding + two control sectors (**status**, **trailer**),
  4 KiB each, at the end of the slot; the anti-rollback floor rides in the
  status sector's tail, past the attempt region. Both modes use the same
  layout, so there is one on-flash shape rather than two.
- The trailer carries an ECDSA signature (COSE algorithm ids — ES256/P-256 by
  default, verified on-device by mbedtls) over a signed `header ‖ JSON-meta`
  region, a SHA-256 of the body, version/identity/provenance metadata, and a
  CRC32. See [trailer.md](trailer.md).
- The status sector carries the trial state machine (`pending` / `confirmed`),
  the **install counter** (`u32 ‖ ~u32`) that orders the two slots, and an
  **attempt region** — one 16-byte marker consumed per trial boot, written
  *before* the app runs, so a trial that *hangs* still makes progress toward
  giving up.
- A frozen, pure `boot.py` (ioctl + computation only) picks the slot: for each,
  verify trailer → signature → body SHA → compatibility → anti-rollback → trial
  state; then boot the survivor with the highest install counter. If none
  survives there is no factory image to retreat to — the device hands off to
  firmware-resident recovery, which re-downloads until it has a working image.

## The one invariant everything rests on

**One erase pass per slot, then writes only.** The installer erases its target
slot once and every write after that is a 1→0 program: the status markers as the
trial advances, the install counter, the rollback log appended to. Nothing is
ever rewritten in place, which is what lets a slot's control data share an erase
block with its body — and that, in turn, is what makes single-image mode possible
at all.

## Design decisions, recorded so they are not re-derived

- **Fall back to the last image that worked, never a factory image.** After
  twenty releases, falling back to what shipped is a regression to code nobody
  has run in years — and reserving a slot for it costs a full image of flash.
  Recovery after a catastrophe lives *in the firmware* (~26 KiB), which a bad
  update cannot erase.
- **The install counter orders the slots, not the version.** It answers "which
  is newer" without consulting the version, so shipping the same version twice
  just works and `reinstall` stays legal. Version remains a human-facing fact
  and an anti-rollback input, never a boot-order input. The counter is written
  *last*, so a half-written image never carries a valid one; a tie prefers
  `confirmed`, then build time, then slot A.
- **The anti-rollback floor gates installs, not the fallback.** `confirm()`
  raises the floor to the running version, so the slot behind an accepted update
  is below the floor *by construction* — rejecting it at boot would delete the
  safety net at the exact moment the device proved it did not need it. A
  **confirmed** slot is therefore exempt at boot; the floor is enforced
  pre-erase in the installer and on any slot not yet confirmed, which is where a
  replayed old release actually arrives.
- **Trials get N attempts (default 3), not one.** A false rejection costs a full
  re-download + erase + write, repeatedly; an extra attempt on a genuinely bad
  image costs one reboot. Retries only help when the failure is self-resetting,
  which is why N stays small — and why the attempt marker is written at boot,
  before the app runs. `N = 1` degenerates exactly to one-shot.
- **User settings live on `/flash`, nowhere else.** The romfs is destroyed by
  every update and the firmware is one image shipped to every device; `/flash`
  is the end user's storage, and its visibility is the point — a stranded device
  becomes recoverable by dropping the recovery network file onto a drive the
  user already mounts ([tutorial page 14](../tutorial/14-recovery.md)). This
  coexists with "nothing *required to boot* lives on `/flash`": a missing
  credentials file is visible and user-repairable; a missing CA was silent and
  was moved out.

## Hardware-proven constraints

Both were found as **silent** failures on the fleet — no fault, no log, no
reset — and both are enforced in the installer with the reasoning in comments at
the point of use:

- **16 bytes is the minimum flash program unit.** A one-byte program hard-faults
  the N6's octal-DTR XSPI with no trace; every marker is a 16-byte write.
- **A bulk memory-mapped read must never reach the last address of an XIP
  device.** On the STM32H7 QUADSPI such a burst wedges the peripheral and every
  later XIP read hangs the AHB forever; slot B ends flush against the end of the
  H7 Plus's flash. Every XIP alias is clamped 512 bytes short of the end
  (`_XIP_TAIL_GUARD`) — reads are shortened, never moved, and only trailer
  padding is affected.

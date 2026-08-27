# Boot and rollback

*[← 10 · Erase & bootloader](10-erase-and-bootloader.md) · [Index](00-introduction.md) · [12 · The device library →](12-device-library.md)*

---

OTA has two pieces that run **on the camera** (everything else — `project`, `build`,
signing — runs on your host): **`boot.py`**, frozen into the firmware by
`build firmware`, which picks and verifies the image to run at every boot; and the
**`openmv_ota`** library your app imports. This page is the boot half — slot
selection, the trial, and rollback. Both pieces are self-contained (they can't import the host
`openmv_ota.ota.*` packages under MicroPython); their pure logic is host-tested,
the device I/O is exercised under QEMU, and the full OTA cycle — install, trial,
confirm, rollback — runs on real cameras in a hardware-in-the-loop fleet before
any device change merges.

## `boot.py` — slot selection at boot

An OTA partition holds **two slots of equal size, A and B**. Neither is special: both are
real, signed, updatable images, and the one that boots is simply the newest valid one. On
every boot `boot.py` runs after the board's stock `_boot.py` and, **for each slot**:

1. Reads the slot's signed [trailer](../reference/trailer.md) and **verifies the ECDSA signature**
   (via the on-device mbedtls shim) *before trusting any header field*.
2. Checks the authenticated header: **integrity** (body SHA-256), **cross-flash guard**
   (`product_id`), **compatibility** (`min_platform_version`), and **anti-rollback**
   (`payload_version` vs the **rollback floor**). `min_platform_version` is the
   image's firmware floor — stamped automatically from the firmware the image was
   built against, so an app compiled for newer firmware (a newer bytecode ABI,
   newer frozen modules) is never mounted by an older one.
3. Applies the **trial rules**.

Then it mounts the surviving slot with the **highest install counter** at `/rom`. It
records the outcome in module globals (`last_slot`, `last_payload_version`,
`last_failure_reason`) for the app to read, since the boot path can't print.

So a bad, corrupt, mis-targeted, downgraded, or un-confirmable update can never strand the
device: it simply is not the newest *valid* slot, and the previous release — which is still
sitting in the other slot, untouched — runs instead. If **no** slot survives there is no
factory image to retreat to; the device hands off to firmware-resident recovery, which
re-downloads until it has a working image.

### The slot's control sectors

Everything the rest of this page describes lives in **two 4 KiB control
sectors** at the end of each slot — the same shape in A/B and single-image
mode:

![A slot's layout: the image body, 0xFF padding, then two control sectors — status (trial markers, the install counter, the attempt region, and the anti-rollback floor in its tail) and the trailer (the signed identity, written at build and verified every boot).](images/slot-layout.svg)

**The `status` sector orders the slots.** Which slot is "newest" is decided by an
**install counter**, not the version: each install stamps a counter one higher than
anything present in either slot into the `status` sector of the slot it writes,
beside the trial markers and the attempt region. It is stored as `u32 ‖ ~u32` —
self-validating, so a torn write reads as *unknown* rather than as some other
number. The version cannot do this job: installing the *same* version twice is
legitimate (a reinstall, a re-flash) and must not be ambiguous, so the version
stays a human-facing fact and an anti-rollback input, never a boot-order input.
A slot whose counter is unreadable is still bootable if it verifies; it just
sorts last, because we cannot claim it is newer than something that says so.

**The floor rides in the `status` sector's tail.** The anti-rollback floor is a
monotonic minimum version, so a device can't be downgraded to an *older signed*
release (a replay attack — the signature is genuine, just stale). The floor must
*rise* at `confirm()`, but flash can only program bits 1→0 — a stored value
cannot be overwritten with a bigger one without an erase. So the floor is held
as appended entries in the region past the attempt markers: raising it programs
a fresh entry into blank bytes, and the current floor is simply the highest
valid entry — a mutable, monotonic value built from write-once flash, not a
history log. In practice the region holds one or two entries between erases:
the floor carried in at install, plus one raise at the first `confirm()`.
`boot.py` takes the **highest entry across both slots** as the floor. Because an
install erases the whole slot it writes, the installer **copies the current
floor forward** into the fresh slot as its *very first write* — before a single
image byte — so the floor is never absent from flash for longer than the erase
itself. That ordering is what makes single-image mode safe: there the erase
just destroyed the only copy, and a slower carry would leave a power-cut window
in which recovery would restart with no floor at all. Without the carry,
rewriting whichever slot happened to hold the highest entry would silently
lower the floor and re-admit a release the device had moved past. The floor is
raised *before* `CONFIRMED` is written, and the crash window between the two is
what the floor's one exemption exists for: **the floor never applies to a
confirmed slot**. Its job is to gate what may be *installed* — a slot the
device already ran and kept is always a legal fallback, even though every kept
update leaves the previous slot below the floor by construction. (The honest
limit, inherent to A/B: an attacker who can force trials to fail can force a
return to that previous confirmed release — never anything older.)

**Why a 4 KiB sector for a few bytes of state.** The sector looks oversized, and
that is the design: nothing in it is ever erased while the device runs.
Installing erases the target slot **once**, leaving every control byte blank
(`0xFF`); from then on every state change — an attempt consumed, `CONFIRMED`
written, a floor entry appended — is a 1→0 program into bytes that are still
blank, which flash permits without an erase. No runtime erase means no
read-modify-write window: power can fail at any byte and the worst case is one
torn, self-invalidating entry. And a 4 KiB reservation costs nothing on a
multi-megabyte partition while buying headroom — the attempt region, the floor
region, blank tail for future metadata — without ever reshaping a layout that
fielded devices depend on.

### Single-image mode

Boards whose entire ROMFS is a single erase sector (OpenMV2/3/4) cannot host two slots, so
they build in **single-image mode**: one slot spanning the partition, no fallback. Erasing
"the target slot" there *is* destroying the running image, which is why firmware-resident
recovery is the enabling piece rather than a nicety — a failed update costs a network round
trip instead of a reboot, and a device that cannot reach the network needs a physical
reflash. Everywhere else A/B is the default and single-image is an explicit opt-out
(`single_image = true`), named for what you get.

## The update lifecycle (and your app's one job)

An installed image is on **trial** until your app says otherwise. Markers in the slot's
status sector drive it, plus an **attempt region** — one marker programmed per trial boot:

![The trial lifecycle: the updater writes the other slot and sets pending; boot 1 mounts it on trial, consuming an attempt; the app validates itself and confirms; later boots mount the committed image. If the trial never confirms, boots 2 and 3 each consume an attempt and boot 4 rejects the slot, mounting the previous release.](images/trial-lifecycle.svg)

So **your app must call `openmv_ota.confirm()` once it has proven itself healthy** —
otherwise the trial eventually gives up and the device returns to the previous release.
Confirm *after* a real health check (sensors up, first frame, your self-test), **not**
blindly at boot, or you defeat the rollback safety.

**A trial gets `max_attempts` boots (default 3), not one.** The costs are lopsided: a false
rejection costs a full re-download, erase and write that the server then offers again —
minutes, traffic and flash wear, repeatedly — while an extra attempt on a genuinely bad
image costs one reboot. The honest limit, and why the default stays low: retries only help a
failure that *self-resets*. A **hang** now hangs N times instead of once. Set
`[ota].max_attempts = 1` for a single-shot trial.

The attempt is recorded **before** the image runs, which is what makes a hang count. Two
subtleties follow from it: if `boot.py` cannot record the attempt (the write fails or won't
verify) it does **not** run that slot — an untracked trial could hang forever with no way to
know to move on — and it drops to the next-newest slot rather than abandoning the boot.

**Updates are deferred while a trial is unconfirmed.** The installer writes the slot you are
*not* running, which during a trial is the last release known to work. Taking a new update
then would trade a proven fallback for an unproven one, at the moment the device has already
said it is unsure of itself — so an offered update waits until you `confirm()`. (Single-image
devices are exempt: there is no fallback to protect, and waiting would strand them.)

## See also

- [CI](../reference/ci.md) — how the QEMU device-I/O tests run.

---

*[← 10 · Erase & bootloader](10-erase-and-bootloader.md) · [Index](00-introduction.md) · [12 · The device library →](12-device-library.md)*

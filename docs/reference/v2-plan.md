# v2 — true A/B, single-image mode, and firmware-resident recovery

Status: **SHIPPED.** Merged to `main` (the `kwabena/v2-step1-mode` branch is long gone), and
proven on hardware: every board in the fleet runs its full regression set against v2 -- see
[v2-hardware-results.md](v2-hardware-results.md). This page is kept for the DESIGN and its
reasoning, which is still what the code implements; it is not a status page.

Progress: mode derivation, the control-block fix, project config, `_ota_config` stamping, the
symmetric A/B `boot.py`, the install path retargeted to the non-running slot, the provisioning
image writing two real slots, the check-in reporting both slots, and the HIL catalog. The docs and
the QEMU suite were swept with it -- the QEMU job was calling `evaluate_slot`'s v1 signature and
would have failed. Per the bench rule, hardware proof is one board and the targeted scenarios
first; the fleet runs afterwards as a regression gate, never as a debugger.

**Still unbuilt, and named honestly:** firmware-resident recovery itself. `boot.py` hands off to
it and the config it needs is stamped into the firmware, but the flow does not exist -- so today a
device with no valid slot halts rather than re-downloading. That only bites single-image boards
and the both-slots-bad case, but it is the piece that makes single-image mode honest.

Two findings that changed the design, recorded so they are not re-derived: the erase block was
sizing control sectors it does not govern (fixed -- `control_block()` is 4 KiB always, which also
made large-erase boards A/B-capable), and firmware-resident recovery costs **~26 KiB of FIRMWARE
flash** measured with `mpy-cross`, not the romfs partition -- so the single-image boards' image
budget is untouched and the mode is viable. The rewrite goes **one verb at a time**
(`project` → `build` → `boot.py` → `romfs`/`flash` → `server`/`client`), because each verb owns a
piece of the on-flash contract and changing two at once makes a failure impossible to attribute.

**No v1 devices are deployed.** There is no field-migration constraint, so layout and formats are
chosen on merit rather than compatibility. This is the single biggest simplification available and
it will not be available later.

## Why v2 exists

MicroPython can now erase at an arbitrary offset. v1 was designed around not being able to, and
three of its constraints fall away:

1. **The factory image is the wrong fallback.** After twenty releases, falling back to what shipped
   is a regression to code nobody has run in years. Fall back to **the last image that worked**.
2. **Reserving a slot for that factory image is expensive**, and impossible on boards with a single
   erase block.
3. **Recovery does not need a romfs.** The factory image existed to run the OTA flow after a
   catastrophic failure. That flow can live **in the firmware** — a fraction of the flash, and it
   cannot be erased by a bad update.

## Invariant that carries over

**One erase pass per slot, then writes/programs only.** Verified against the current installer:
`erase(total)` walks the slot once via `rom_ioctl(3, …)`, and there is no erase call anywhere after
`install: writing FRONT` — the write loop only programs and reads back. Both v2 modes reduce to the
same shape: erase the target slot once, stream writes into it.

Consequence for single-image mode, stated plainly: on a board whose partition **is** one erase
block, "erase the slot" **is** "destroy the running image". There is no gentler option — which is
exactly why firmware-resident recovery is the enabling piece, not a nicety.

## The three modes

### Mode selection

**A/B is the default for every board with room.** Single-image exists for the legacy one-sector
boards and is a curiosity there more than a mode anyone is expected to ship — so it must work, but
it does not get to shape the design.

Selectable, but deliberately **not symmetric**, because the two choices are not equally safe:

- **Derived by default** — A/B whenever the geometry allows, single-image only when it does not. The
  safe path requires no decision.
- **Opting out is explicit and self-describing** — `single_image = true`, not `ab = false`. The
  latter reads as a preference; the former forces the consequence into the name, and the comment
  states it: *a failed update then requires a network round trip to recover, and a device that
  cannot reach the network needs physical reflashing.*
- **Requesting `ab` on a board without room is a build ERROR** naming the shortfall — never a silent
  downgrade to single-image.

The trade is real in both directions: a second slot costs a full image of flash that a maker may
want for models or data. What makes it a footgun is not the cost but the *timing* — the cost of
opting out is invisible when you choose and expensive years later, on hardware you cannot reach.
Same principle as `ALLOW_STM32_IWDG`: safe path automatic, unsafe path explicit and named.

### A/B — the default mode

Both slots hold real, signed, verifiable images. `boot.py` boots **the newest valid one**. No
mcuboot-style shuffling: nothing is copied or swapped, so a power cut cannot leave a half-moved
image. Promotion is a status write, not a data move.

**Ordering: an install counter, not the version.**

The status sector today is 16-byte markers at `PENDING(0)`, `TRIED(16)`, `CONFIRMED(32)`,
`REPR(48)`. **Offset 64 onward is free.** Add an install counter there as `u32 value || u32 ~value`
— the same self-validating pattern the rollback sector already uses, so a torn write is *detectable*
rather than believed.

- **Install target** = the slot with the **lower** counter (the old one; never the running image).
- **On install**, write `counter = other + 1`, **last**, with the status markers — so a half-written
  image never carries a valid counter.
- **Boot** = highest counter among slots that are valid and not failed trials.

Why a counter rather than version or timestamp: it answers "which is newer" without consulting the
version, so **shipping the same version twice just works** and `reinstall` stays legal. Version
remains a human-facing fact and an anti-rollback input, never a boot-order input. If two counters
ever tie (factory-fresh, or corruption): prefer `CONFIRMED`, then `BUILD_TIME`, then slot A.

**Boot selection:**
1. Read both slots' trailer + status.
2. Validate each: magic, signature, sha256, rollback floor.
3. Drop any failed trial (see below).
4. Boot the survivor with the highest counter.
5. None → recovery.

**The floor gates INSTALLS, not the fallback.** `confirm()` raises the floor to the running
version, so the slot behind an accepted update is below the floor *by construction*. Applying
anti-rollback to it at boot deletes the safety net at the exact moment the device finished
proving it did not need it — leaving it one bad update from having nothing to return to.
(Hardware found this: a Nicla that confirmed 1.1.0 then logged `boot: rejected A:rollback`.)
So a **confirmed** slot is exempt at boot; the floor is enforced pre-erase in the installer and
on any slot not yet confirmed, which is where a replayed old release actually arrives.

**Security note.** In v1 a failed trial fell back to the *factory* image; under A/B it falls back to
the *previous update*. An attacker who can force a trial to fail can still force a downgrade — that
is inherent to A/B and mcuboot shares it — but the floor is now "last working release" rather than
"what shipped years ago". Strictly better; recorded so A/B is not misread as weakening rollback.

### Trials: a retry counter, not one-shot

v1 gives a new image exactly one boot to confirm. v2 gives it **N (default 3, configurable)**.

The costs are wildly unequal: a **false rejection** costs a full re-download + erase + write, which
the server then offers again — minutes, traffic and flash wear, repeatedly. An **extra attempt on a
genuinely bad image** costs one reboot. Cheap insurance against an expensive mistake, and transient
boot failures are real on this hardware (a camera sensor that fails to initialise depending on cable
length is already documented in this project).

**The honest limit**, which is why the default stays low: retries only help when the failure is
self-resetting (crash → reboot). A **hang** now hangs N times instead of once. So keep N small and
configurable, and **write the attempt record at boot, before running the app** — then a hang still
consumes an attempt and the trial makes progress toward giving up.

**Implementation fits the write-only constraint** using an idiom already in the tree: an **append
region** where each boot consumes one entry, exactly like `_rollback_append_offset` (scan for the
first blank slot, write it). Attempts used = entries consumed. No erase, no read-modify-write, and a
torn write costs one attempt rather than corrupting a count.

`TRIED` becomes "attempts ≥ 1"; rejection becomes "attempts ≥ limit && !CONFIRMED". **N = 1
degenerates exactly to today's behaviour**, so this is a superset and the conservative option stays
available per product.

### Single-image — one erase block (legacy cams only)

One slot, no fallback. **A failed trial means recovery**, not a reboot into the other slot — so a bad
image costs a network round trip rather than a reboot. That is the mode's real price and it belongs in
the docs.

Scope note: the boards this exists for are old, so this is closer to a curiosity than a mode with
real deployments behind it. It must work, but it does not get to constrain the A/B design, and its
flash budget is not a gate on the project.

### Recovery — all modes

No runnable image → the **firmware-resident OTA flow**: bring up the network, check in, download,
install, reboot. Repeat until a working image exists. What the factory image used to do, minus the
flash cost.

## Configuration: who supplies what

**Mechanical (build-stamped or hardware) — no user input.** `_ota_config` already carries
`PARTITION_SIZE`, `FRONT_SIZE`, `OTA_BLOCK`, `PRODUCT_ID`, `ACCOUNT_ID`, `PLATFORM_VERSION`,
`BUILD_TIME`, `TRUSTED_KEYS`. Device id comes from the hardware UID. `poll_after_s` has a default the
server overrides per response; `ntp_host` defaults to `pool.ntp.org` plus a rotating IP fallback.
There is **no device-side API token** — check-in is authorised by what it is, and the
registration/entitlement gate is server-side.

**Decided: the update server URL and the TLS CA move into the firmware**, stamped by the build
alongside `_ota_config`. They are the maker's, constant per build, and forcing the romfs to supply
them is precisely what made recovery impossible.

**That leaves exactly one user-supplied item: the WiFi credentials** — the only thing that belongs to
the **end user** rather than the maker, which is why it cannot be build-stamped.

### Where user settings live, and why not elsewhere

Ownership decides it:

| area | owner | rebuilt by |
|---|---|---|
| firmware | maker's code + constants (server URL, CA) | firmware updates |
| romfs | maker's app | **every OTA** |
| `/flash` | **the end user's data** | nobody |

So user settings belong in **neither** the romfs (destroyed by every update) nor the firmware (one
image shipped to every device — it cannot hold per-device data). They go on `/flash`.

**The MBR spare-bytes idea does not work**, and there is direct evidence: our own `/flash` recovery
erases exactly that sector — `flash/imx.py` *"wipe the user disk's first sector (its MBR)"*, documented
in [the flashing page](../tutorial/04-flashing.md). Add host OSes that "repair" MBRs on a USB-MSC device, and any reformat, and it is
a trap rather than a trick.

**`/flash` being user-visible is a feature.** A device stranded because the user changed their WiFi
*after* an update broke it becomes recoverable by **dropping a file onto the drive** — no reflash. That
is worth designing for.

**Reconciling with "nothing required may live on `/flash`"** (the rule that moved the CA into the
romfs): the distinction is *visibility and repairability*, not criticality. A missing CA was silent and
un-fixable by a user; a missing credentials file is visible on a drive they already mount and they can
restore it themselves. Different failure class, so the same storage is right here and wrong there.

### The recovery network file

Hand-edited by a human in a bad situation, so **`key = value` with `#` comments**, not JSON. JSON has
no comments (we cannot ship documented defaults) and its failure mode is a silent parse error from a
stray comma or a smart quote — on the one path with no other way in. The parser is ~10 lines and
allocation-light, which matters on the recovery path.

```ini
# OpenMV recovery network settings — ONLY used if the device cannot OTA any other way.
# Safe to delete. Edit, save, re-plug the drive.
interface   = wifi        # wifi | eth
wifi.ssid   = MyNetwork
wifi.psk    = secret      # replaced with an encrypted form on next boot
ipv4        = dhcp        # dhcp | static
# ipv4.address = 192.168.1.50
# ipv4.netmask = 255.255.255.0
# ipv4.gateway = 192.168.1.1
```

- **SSID stays plaintext.** It is broadcast in every beacon, so encrypting it protects nothing — and it
  is *the* diagnostic ("this is still pointed at the old router"), the most likely cause of a stranded
  device. Only the PSK is encrypted. (Caveat: SSIDs are geolocatable, so a whole-blob opt-in may suit a
  specific customer later.)
- **PSK encrypted with the device UID = obfuscation, not security**, and should be documented as such.
  Anyone with USB access can defeat it. It is still a strict improvement on the status quo, where the
  PSK sits in plaintext in `main.py` inside the romfs.
- **On boot, rewrite the plaintext PSK as the encrypted form.** Do **not** auto-delete the file: it is
  exactly what is needed *next* time, and silently removing a user's config is surprising. Rewriting
  gets the hygiene without destroying the capability.
- **Two copies plus a checksum**, written one at a time, so a crash mid-write costs one copy not both.
  Writes only happen when credentials change, so the exposure is small.
- **Never give up permanently.** If the file is missing: try DHCP on Ethernet if present, then re-read
  on a slow retry — so dropping the file onto the mounted drive fixes a **live** device with no power
  cycle, and a cable plugged in later is picked up.

## Sequencing (one verb at a time)

1. **`project`** — mode (`ab` | `single`) per board; carry server URL + CA into the build. No
   behaviour change.
2. **`build`** — stamp mode + recovery config into `_ota_config`; freeze the OTA flow into firmware.
   Prove the firmware still boots and the v1 path is untouched. **Measure the flash cost here** — it sizes the
   recovery flow and tells us whether the legacy single-sector boards can host it. No longer a
   gate on the project, since A/B is the default everywhere with room.
3. **`boot.py`** — install counter, two-slot validation, newest-valid selection, trial retry counter,
   hand-off to recovery.
4. **`romfs`/`flash`** — layout for two real slots and for single-image; drop the factory special case.
   Three things fell out of building it that were not obvious from the design:
   - **The two A/B slots must be the SAME SIZE.** One OTA image has to be installable into
     *either* slot (the installer picks the target at run time) and the image is slot-sized with
     its trailer in the last block. A partition whose half does not divide evenly leaves a
     remainder below the split, deliberately unused, rather than handing it to B.
   - **SINGLE keeps the identical four-sector control layout**, rather than packing the records
     into one block. The saving was 12 KiB on boards that are a curiosity; the cost would have
     been a second on-flash layout for boot.py, the installer and the builder to agree on
     forever. (The 4 KiB `control_block()` fix is what made 16 KiB affordable on a 128 KiB
     partition — 112 KiB left for the image, exactly the budget those boards always had.)
   - **`confirm()` and `status()` had to move to the RUNNING slot**, and `trial` had to stop
     requiring `TRIED`. boot.py no longer writes TRIED (attempts are counted in the append
     region), so the v1 predicate would have made *no* trial confirmable — every update would
     have rolled back. The v1 "only confirm if we booted FRONT" guard is now structural: the
     caller reads the running slot's own sector, so there is no sector for a slot we did not boot.
5. **`server`/`client`** — check-in payload carrying both slots (versions + counters) so the server can
   distinguish "trial in progress" from "settled"; then the cloud work.
6. **HIL catalog** — every scenario ending "settled back on golden" re-expressed as "rejected and kept
   retrying". The integrity gates (bad_sig, bad_key, sha, anti-rollback) are unchanged; only the
   *recovery* assertion moves.
   **Known stale, and deliberately left for this step:** the `boot.mount.front` / `boot.mount.back` /
   `boot.front_reject` coverage keys still match on the words FRONT and BACK, which boot.py no longer
   logs (it logs `mounted A` / `mounted B`). The static guard cannot catch it, because it strips a
   trailing runtime slot name by design. Those three markers carry scenario *meaning* — "ended on
   golden" is what several scenarios assert — so renaming them is the catalog rework, not a
   search-and-replace. Until step 6 lands, a bench run would silently fail to see them.

Each step proves itself on **one board** before the fleet runs as a regression check.

## Open items

- **Recovery flash cost** — measure at step 2. Informs whether the legacy single-sector boards can
  host the flow; not a gate on A/B, which is the default everywhere with room.
- **Watchdog during install/recovery** — deferred deliberately. Arming is unresolved on the N6's tight
  WWDG ceiling (~167 ms vs a 65–100 ms collect); `relax()` remains an option.
- **Credentials hand-off API** — how the app passes proven-good credentials to the OTA layer; expected
  to fall out of the build rather than needing a decision now.
- **Enterprise WiFi / captive portals** — SSID+PSK does not cover them; out of scope unless asked.

# openmv-ota — what's next

Live list of what's being built next. **Done work is not tracked here** — see `git log`.
(It used to be, which is how this file grew into a changelog nobody read. If you want the
history of the multi-tenancy, API-audit or signer-hygiene work, the commits have it in
more detail than a bullet ever will.)

## Parked — revisit, with the context needed to pick each up

Each of these was understood and deliberately set down, not forgotten. The reason it was
parked is recorded so the next person does not re-derive it.

- **DER trust store, and dropping the PEM parser.** PEM is base64-wrapped DER, so DER is
  ~25% smaller — but mbedtls only concatenates PEM: *"For certificates in PEM encoding,
  this may be a concatenation of multiple certificates; for DER encoding, the buffer must
  [be a single certificate]"* (`x509_crt.h`). So the public bundle cannot be one DER blob
  without splitting it per-cert on device. The saving also lands in the wrong place: the
  bundle only ships on boards with 4–12 MiB slots, where ~49 KB is noise, and a supplied
  root is ~1 KB where DER saves a few hundred bytes.
  **The version worth doing is about CODE size, not data.** An OTA build patches in
  `MBEDTLS_BASE64_C` + `MBEDTLS_PEM_PARSE_C` purely to parse PEM. A project supplying a DER
  root and shipping no PEM bundle needs neither — and that is `FLASH_TEXT`, which is exactly
  where the pressure is (OPENMV4 is 32,192 bytes over; the H7 Plus hit 106% when the bundle
  was briefly frozen). Measure the saving from dropping those two defines before building
  anything: if it is ~3 KB it does not dent 32 KB, if it is ~10 KB it is worth having.

- **`reinstall` fails on the N6 (lan).** 10/11 of that leg passes. The device logs
  `install: erasing block XIP` and goes silent for nine minutes; a 12 MiB erase normally
  takes **73 s**. Every *passing* install starts at ~6.4 s uptime (right after a boot); this
  one starts at 22.8 s, from a steady-state app — which is the field case. It also passed
  once at 1049 s against a 1200 s timeout, so "merely slow" is not excluded. The XIP erase
  path now reports progress every 64 blocks (like the block-device path), which distinguishes
  hang-at-block-N from slow in a single run — that is the next step, on one board.

- **The AE3 reset-loops after a watchdog leg.** It can arm a watchdog for the first time
  (the alif carry works again), and the armed app then bites, reboots into itself, and loops.
  100 ms is the N6's WWDG ceiling; `openmv_wdt`'s own notes say the alif WDT has far more
  headroom, so a per-port window is the likely answer. Safety-relevant — measure, do not
  guess. The loop is no longer fatal to a run (the flash recovers through the DFU window).

- **Firmware updates via the ROMFS.** Design discussed, not built: the bootloader copies a
  verified `firmware.bin` out of a slot into the firmware area, so firmware ships only when
  it changes and needs no A/B of its own (there is no room for one). The framing that makes
  it work: the bootloader is a *reconciler*, not an installer — the verified slot is the
  durable source, so a power loss mid-copy is retried, not a brick. Open points: reconcile
  only from a **confirmed** slot (so firmware never moves on an unproven image), never
  downgrade, gate apps with `min_platform_version`, put the image at a fixed offset so the
  bootloader needs no romfs parser, keep the bootloader itself out of OTA scope.

- **The harness scores before the device settles.** A happy-path leg can flake when the
  reset that opens the scored window lands mid-erase: the half-written slot legitimately
  falls back and `boot.fallback` is forbidden. Seen ~1 in 14 runs, on the RT and the N6.

## Remaining / optional (pre-v2, still true)

- **Real-hardware/cloud acceptance** for the signer backends (SoftHSM opt-in test exists;
  AWS/GCP/Azure + provisioning are unit-covered via fakes but need one live end-to-end pass
  each).
- **KMS bulk-provisioning cost** — `keys backend provision` defaults to a small pool (1
  factory + 4 ota) because external keys are billable; document per-provider pricing before
  recommending it.
- `configure` with an explicit `DIR` must place it *before* `--set`/`--backend` (argparse
  can't split two positionals across value options); the default `.` covers the common
  in-project case.
- Consistent error envelopes (FastAPI's `{detail}` is the current shape).
- Account-scoped read indexes lag multi-tenancy (indexes lead with `product_id`, not
  `account_id`); additive index-only migration when fleets/accounts grow.

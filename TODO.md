# openmv-ota — what's next

To-do only. Done work is in `git log`; deliberate non-goals are in
[docs/compliance/residual-threats.md](docs/compliance/residual-threats.md); this
file's own history has the longer design notes behind each line.

- **Verify the coprocessor image still installs (AE3)** — the OTA modules are no
  longer frozen into the M55_HE core (it has no mbedtls, verifies nothing, and was
  carrying an 18 KB installer it cannot run — see `_per_core_freeze`). Its romfs
  should mount by MicroPython's own auto-mount, and the scaffolded coprocessor app
  imports nothing of ours, but that has never been exercised end to end: flash an
  AE3, run `openmv_ota.sync()`, and prove the helper partition is written and the
  helper core boots its app.
- **The Nicla hangs mid-download (terminal)** — seen once, 2026-08-05, on
  `ARDUINO_NICLA_VISION` wifi, scenario `full`: 4 KB written, then total silence
  until the run timed out. No reboot, no timeout, no retry, no fallback. Suspect a
  blocking mbedtls read. Under the current design golden catches it; once the golden
  image is dropped (see *Firmware updates via the ROMFS*) a device that hangs has no
  image and never asks for another, so this is a launch blocker rather than a
  curiosity. Deliberately not closed as a flake — grep the run log for
  `install: 0% (4096/` followed by nothing.
- **The check-in rate limit keys on the whole IP** — `app.py` passes
  `request.client.host` straight to `ratelimit.allow()`, so an IPv6 caller gets a
  fresh bucket per address and a /64 (what any connection is handed) is 2**64 of
  them. The check-in and feedback limits are one address rotation from meaningless.
  The same limiter also only sweeps windows older than 60 s, so a flood of distinct
  addresses inside one window grows the table past `max_tracked` unchecked. Fixed on
  the cloud website 2026-09-23 (`limit_key()` collapses IPv6 to its /64 and the sweep
  evicts the quietest half when it frees nothing); this server carries the same shape
  and has not been changed, because its callers are devices and that deserves its own
  look at what a shared NAT does to a fleet behind one address.
- **Device lockdown** — debug-port and boot protection (residual-threats:
  planned); until then bench/bus access is accepted.
- **Firmware updates via the ROMFS** — bootloader as *reconciler*: copy a
  verified `firmware.bin` out of a **confirmed** slot into the firmware area at
  a fixed offset (no romfs parser in the bootloader), never downgrade; a power
  loss mid-copy retries, not bricks.
- **Rollout ramps** — optional declared-at-creation stages
  `{percent, min_soak, min_attempted, max_failure_rate}` evaluated lazily on
  check-ins; auto-pause always beats auto-raise; every auto-raise audited.
- **Scaling past ~100K devices** — metastore connection pool, `poll_after_s`
  jitter (post-outage herds), NAT-aware rate limiting (per-IP × per-worker
  today).
- **`device retire`** — no verb removes a device record today.
- **H7 Plus (OPENMV4P): the WINC wedges after a watchdog bite (terminal)** — the
  one board in `WATCHDOG_BROKEN`. NOT the watchdog window: measured on hardware
  2026-08-02, `machine.WDT("WWDG", 100)` arms on the H743, a 20 ms feed loop and
  `relax()`'s ISR feed both survive, and the board does not reset-loop. What
  actually happens is that the armed leg bites mid-install, resets, and then the
  WINC is wedged — 39 consecutive `OSError(22)` (EINVAL) check-ins, preceded by one
  `MBEDTLS_ERR_SSL_INVALID_MAC` and one `TypeError` — and never recovers, so no
  install ever runs again. A WINC driver/socket-state problem. Same reasoning as the
  Nicla hang: a network stack that never recovers means a device with no image never
  gets one. Three WINC fixes are parked on openmv branches that may bear on it
  (`winc_reconnect`, `winc_bounded_waits`, `winc_19_7_11`) — try those before
  theorising.
- **Signer backends: one live pass each** — AWS/GCP/Azure KMS + provisioning
  are unit-covered via fakes (SoftHSM has an opt-in real test); each needs one
  end-to-end run against the real service.
- **KMS provisioning pricing** — `keys backend provision` defaults to a small
  pool because external keys are billable; document per-provider pricing before
  recommending bigger pools.
- **DER trust store — measure first** — the win is CODE size, not data:
  dropping `MBEDTLS_BASE64_C` + `MBEDTLS_PEM_PARSE_C` from OTA builds that ship
  a DER root and no PEM bundle frees `FLASH_TEXT` (where OPENMV4 is 32 KB
  over). Measure the saving before building anything.
- **Account-scoped read indexes** — indexes lead with `product_id`, not
  `account_id`; additive index-only migration when fleets/accounts grow.

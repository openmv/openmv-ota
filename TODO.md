# openmv-ota — what's next

To-do only. Done work is in `git log`; deliberate non-goals are in
[docs/compliance/residual-threats.md](docs/compliance/residual-threats.md); this
file's own history has the longer design notes behind each line.

- **On-device image encryption** — images are signed, not confidential
  (residual-threats: planned). The trailer's `reserved0` field is the headroom
  for its metadata.
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
- **H7 Plus (OPENMV4P) armed watchdog** — the one board in `WATCHDOG_BROKEN`:
  boot + app startup does not fit the 100 ms WWDG ceiling, so one bite becomes
  a reset loop. Likely a per-port window; safety-relevant — measure, don't
  guess.
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

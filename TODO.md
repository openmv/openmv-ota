# openmv-ota — what's next

Live list of what's being built next. **Done work is not tracked here** — see `git log`.
(It used to be, which is how this file grew into a changelog nobody read. If you want the
history of the multi-tenancy, API-audit or signer-hygiene work, the commits have it in
more detail than a bullet ever will.)

## In progress: v2 — true A/B, single-image mode, firmware-resident recovery

The design, the reasoning, and the step-by-step sequencing live in
**[docs/v2-plan.md](docs/v2-plan.md)**. Short version: two equal, updatable slots ordered by
an install counter; no golden image; a failed update falls back to the last release that
*worked* rather than to what shipped years ago.

Steps 1–6 are built (mode derivation → `_ota_config` stamping → symmetric `boot.py` →
install retargeting → the check-in's slot report → the HIL catalog). What remains:

- **Hardware proof.** Nothing in v2 has run on a board yet. Per the bench rule: one board
  and the targeted scenarios first, and the full fleet only afterwards, as a regression
  gate — not as a debugger.
- **Firmware-resident recovery** itself. `boot.py` hands off to it and the config it needs
  is stamped into the firmware, but the flow is not written. Until it is, a device with no
  valid slot halts instead of re-downloading — which only affects single-image boards and
  the both-slots-bad case, but it is the piece that makes single-image mode honest.
- **WiFi credentials for recovery** — the one setting that cannot be baked at build time.
  Design is settled (a hand-editable file on `/flash`, plaintext SSID, encrypted PSK); not
  built.

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

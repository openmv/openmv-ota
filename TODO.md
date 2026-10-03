# openmv-ota — what's next

To-do only. Done work is in `git log`; deliberate non-goals are in
[docs/compliance/residual-threats.md](docs/compliance/residual-threats.md); this
file's own history has the longer design notes behind each line.

- **Device lockdown** — debug-port and boot protection (residual-threats:
  planned); until then bench/bus access is accepted.
- **Firmware updates via the ROMFS** — bootloader as *reconciler*: copy a
  verified `firmware.bin` out of a **confirmed** slot into the firmware area at
  a fixed offset (no romfs parser in the bootloader), never downgrade; a power
  loss mid-copy retries, not bricks.
- **Signer backends: Azure live pass** — GCP KMS and AWS KMS passed live (provision +
  signed `build romfs` + device-check verify; each re-checked by a keyless CI job).
  Azure Key Vault is covered by a fake only (documented in tutorial 05) until an
  account is available.

## Before the hosted cloud goes live (required)

Deferred to ship the Arduino board demo; the service is pre-launch, so data can be reset
when these land. Website parts are in openmv-cloud's `TODO.md`; registrar parts in
openmv-swd-ids'.

- **Turn the unregistered-board switch off** — the pre-launch server switch that treats
  Arduino board types (Nicla Vision, Giga, Portenta, ...) as registered for every account
  must go off at launch, replaced by the claim flow below.
- **Arduino board claiming** — proof of possession: `flash factory` (logged in) reads the
  board's chip id over USB and registers it to the account; `client device claim <id>` for
  boards flashed elsewhere. One account per board; release/transfer path; staff dispute
  resolution (re-flash while logged in proves possession). Server treats a claimed board as
  registered only when the check-in's account matches the claim.
- **Claim abuse limits** — claims count against the plan's device limit; per-account and
  per-IP rate limits; only known board types claimable; audit every claim/release.
- **Per-device secrets + signed check-ins (all boards)** — a device's identity today is its
  chip id, which is not secret, so check-ins/logs/telemetry can be spoofed for a known id.
  Issue a per-device secret at claim/registration and verify a signature on every check-in.
  Open question: where the secret lives (not the shared ROMFS; not /flash) — a reserved
  sector written once at flash time, or the secure element below.
- **Secure-element attestation (Arduino)** — Portenta H7 / Nicla Vision carry an NXP SE050,
  Giga an ATECC608, each with factory keys + vendor certificates: sign a server challenge,
  verify the vendor chain. Unclonable identity and the cleanest proof of possession;
  replaces the stored secret where the chip exists.
- **PyPI release** — only after the Getting started flow passes on all six boards from a
  clean venv installed off a local wheel (no dev checkout).
- **Pre-launch cleanup** — revoke the test OTA tokens used for board bring-up; log the
  client out on the HIL nodes (`~/cloudtest/xdg`, the AE3's `~/.config/openmv-ota`).

## Hardening (after the functionality is done)

An external audit (and SOC 2, which certifies company controls, not code) is out of
budget for now; these are the free/cheap passes to run first, in order.

- ~~**GitHub-native scanning**~~ — DONE 2026-09-28: CodeQL (`security-extended`, Python + the
  C shim), Dependabot (pip + actions), Scorecard, every action SHA-pinned, workflow tokens
  read-only by default. Fork PRs can no longer reach the self-hosted bench. Left for the repo
  owner: switch on secret scanning + push protection and Dependabot alerts in Settings.
- ~~**Stricter lint + dependency audit**~~ — DONE 2026-09-28: ruff `S` + `B` enforced (each
  suppression carries its reason); pip-audit job in CI.
- **Fuzz the attacker-facing parsers** — Hypothesis half DONE 2026-09-28 (#94): trailer,
  manifest, romfs, delta, installer HTTP, csi poll and publish, 20k examples per property; 11
  bugs fixed, no signature/anti-rollback bypass. Left: the C ECDSA shim under libFuzzer with
  ASan/UBSan; the server API via Schemathesis from its OpenAPI schema.
- **Scan the web app** — OWASP ZAP baseline against the fleet simulator.
- **Internal red-team passes** — `/security-review`, and `/code-review ultra` on the
  signing/verify/installer/anti-rollback path.
- **When there is budget** — a targeted pentest of the update + signing chain only.

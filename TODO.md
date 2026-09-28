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

## Hardening (after the functionality is done)

An external audit (and SOC 2, which certifies company controls, not code) is out of
budget for now; these are the free/cheap passes to run first, in order.

- ~~**GitHub-native scanning**~~ — DONE 2026-09-28: CodeQL (`security-extended`, Python + the
  C shim), Dependabot (pip + actions), Scorecard, every action SHA-pinned, workflow tokens
  read-only by default. Fork PRs can no longer reach the self-hosted bench. Left for the repo
  owner: switch on secret scanning + push protection and Dependabot alerts in Settings.
- ~~**Stricter lint + dependency audit**~~ — DONE 2026-09-28: ruff `S` + `B` enforced (each
  suppression carries its reason); pip-audit job in CI.
- **Fuzz the attacker-facing parsers** — trailer, manifest, romfs and delta-patch parsers
  (Hypothesis properties + atheris); the C ECDSA shim under libFuzzer with ASan/UBSan; the
  server API via Schemathesis from its OpenAPI schema.
- **Scan the web app** — OWASP ZAP baseline against the fleet simulator.
- **Internal red-team passes** — `/security-review`, and `/code-review ultra` on the
  signing/verify/installer/anti-rollback path.
- **When there is budget** — a targeted pentest of the update + signing chain only.

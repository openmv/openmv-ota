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
- **Signer backends: AWS + Azure live pass** — GCP KMS passed live (provision +
  signed `build romfs` + device-check verify, 2026-09-27; re-runnable opt-in
  test). AWS KMS and Azure Key Vault are covered by fakes only (documented as
  such in tutorial 05) until someone with accounts runs one pass each.

## Hardening (after the functionality is done)

An external audit (and SOC 2, which certifies company controls, not code) is out of
budget for now; these are the free/cheap passes to run first, in order.

- **GitHub-native scanning** — CodeQL (`security-extended`, Python + the C verify shim),
  secret scanning + push protection, Dependabot, OpenSSF Scorecard (pin Actions to SHAs,
  least-privilege workflow tokens). openmv-cloud is private: Semgrep OSS instead of CodeQL.
- **Stricter lint + dependency audit** — ruff `S` (Bandit) and `B` rule sets; pip-audit /
  OSV-Scanner on our own dependencies in CI.
- **Fuzz the attacker-facing parsers** — trailer, manifest, romfs and delta-patch parsers
  (Hypothesis properties + atheris); the C ECDSA shim under libFuzzer with ASan/UBSan; the
  server API via Schemathesis from its OpenAPI schema.
- **Scan the web app** — OWASP ZAP baseline against the fleet simulator.
- **Internal red-team passes** — `/security-review`, and `/code-review ultra` on the
  signing/verify/installer/anti-rollback path.
- **When there is budget** — a targeted pentest of the update + signing chain only.

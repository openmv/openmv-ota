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

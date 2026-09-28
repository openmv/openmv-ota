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
- **Signer backends: one live pass each** — AWS/GCP/Azure KMS + provisioning
  are unit-covered via fakes (SoftHSM has an opt-in real test). GCP is next (a
  live pass against a real key ring); AWS and Azure need accounts first, and
  until then ship documented as verified against fakes only.

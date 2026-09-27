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
- **Scaling past ~100K devices** — metastore connection pool, NAT-aware rate
  limiting (per-IP × per-worker today). (`poll_after_s` jitter for post-outage
  herds is done — `poll_jitter`.)
- **Signer backends: one live pass each** — AWS/GCP/Azure KMS + provisioning
  are unit-covered via fakes (SoftHSM has an opt-in real test); each needs one
  end-to-end run against the real service.

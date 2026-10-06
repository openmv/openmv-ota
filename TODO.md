# openmv-ota — what's next

To-do only. Done work is in `git log`; deliberate non-goals are in
[docs/compliance/residual-threats.md](docs/compliance/residual-threats.md); this
file's own history has the longer design notes behind each line.

- **Firmware updates via the ROMFS (next)** — bootloader as *reconciler*: copy a
  verified `firmware.bin` out of a **confirmed** slot into the firmware area at
  a fixed offset (no romfs parser in the bootloader), never downgrade; a power
  loss mid-copy retries, not bricks.
- **Device lockdown** — debug-port and boot protection (residual-threats:
  planned); until then bench/bus access is accepted. It is also what makes a stored
  device key unreadable on the boards without a secure element (N6, AE3; see below).

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
- **Secure-element drivers (first)** — pure-Python drivers in the device package, on
  `machine.I2C` / `SoftI2C`: the NXP SE050 (T=1 over I2C, APDUs) and the Microchip ATECC608
  (its wake pulse needs the pin or `SoftI2C`). Demo apps on top: sign a challenge, read the
  public key and any certificates. Where the chips are: the RT1062's SE050C1 at `I2C(2)` 0x48
  (beside the accelerometer at 0x15); Nicla Vision and newer Portenta H7, SE050; Giga R1 and
  older Portenta H7, ATECC608. Read each chip before designing: which factory keys and vendor
  certificates it really carries varies by part, and Arduino's ATECC608 boards often ship
  unprovisioned.
- **Per-device keys + signed check-ins (all boards)** — a device's identity today is its chip
  id, which is not secret, so check-ins/logs/telemetry can be spoofed for a known id. Every
  check-in carries a signature over the request and a fresh server value (a nonce, or a
  timestamp and counter), verified against the key registered when the board was claimed;
  the Live and datalake grants already hang off the check-in, so this secures them too, with
  no change to TLS. Per board: the secure-element boards sign with a chip key that never
  leaves the chip; the N6 keeps a device key wrapped by its hardware unique key (DHUK) --
  bound to the chip, but readable by code on the device until lockdown or a TrustZone signer;
  the AE3's Secure Enclave has a factory ECC key but no service to sign with it (ask Alif),
  so it uses a stored device key like the N6, with its factory public key as a chip-bound
  serial.
- **Pre-launch cleanup** — revoke the test OTA tokens used for board bring-up; log the
  client out on the HIL nodes (`~/cloudtest/xdg`, the AE3's `~/.config/openmv-ota`).

## Hardening (after the functionality is done)

An external audit (and SOC 2, which certifies company controls, not code) is out of
budget for now; these are the free/cheap passes to run first, in order.

- **Fuzz the rest** — the C ECDSA shim under libFuzzer with ASan/UBSan; the server API via
  Schemathesis from its OpenAPI schema.
- **Scan the web app** — OWASP ZAP baseline against the fleet simulator.
- **Internal red-team passes** — `/security-review`, and `/code-review ultra` on the
  signing/verify/installer/anti-rollback path.
- **When there is budget** — a targeted pentest of the update + signing chain only.

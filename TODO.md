# openmv-ota — what's next

To-do only. Done work is in `git log`; deliberate non-goals are in
[docs/compliance/residual-threats.md](docs/compliance/residual-threats.md); this
file's own history has the longer design notes behind each line.

- **Firmware updates via the ROMFS (next)** — bootloader as *reconciler*: copy a
  verified `firmware.bin` out of a **confirmed** slot into the firmware area at
  a fixed offset (no romfs parser in the bootloader), never downgrade; a power
  loss mid-copy retries, not bricks.
- **Device lockdown** — debug-port and boot protection (residual-threats:
  planned); until then bench/bus access is accepted. It is also what makes a camera's
  keys unreadable on the boards that keep them in flash unsealed (the M4, M7, H7, H7 Plus,
  Pure Thermal and AE3; the N6 seals its keys to the chip).

## Before the hosted cloud goes live (required)

The service is pre-launch, so data can be reset when these land. Website parts are in openmv-cloud's `TODO.md`; registrar parts in
openmv-swd-ids'.

- **Delete the device-trust switches** — the hosted server requires signed check-ins and no
  longer serves unclaimed Arduino boards as registered (switched 2026-10-15), and the demo
  projects use `[ota].per_camera_encryption`. Once that is stable, delete
  `OPENMV_OTA_REQUIRE_SIGNED_CHECKINS` and `OPENMV_OTA_SERVE_UNREGISTERED_BOARDS` (their on/off
  paths, tests and docs) and `render.yaml`'s two entries. The M4 (OPENMV2) builds without check-in
  signing (no room in its flash), so it gets update offers only.
- **WINC firmware PRs to openmv** — the H7 and H7 Plus on the hosted cloud run WINC firmware
  that isn't in openmv yet: branch `kwabena/winc_poll`, six commits, to send as small reviewable
  PRs. The big one is async support -- `poll()` and non-blocking stream sockets, which asyncio
  and the openmv_cloud SDK need -- restructured into steps; with it, a real non-blocking `recv`
  (on master `setblocking(False)` is a 0 timeout, which the WINC API reads as "wait forever", and
  `winc_async_request()` never checks its own timeout). The rest are small: SPI transfer errors,
  DNS error codes, a reset clearing the connection state, ETIMEDOUT vs EAGAIN, and an unrelated
  gen_linker fix. Once they land, drop the installer's WINC `relax()` around each recv.
- **Pre-launch cleanup** — revoke the test OTA tokens used for board bring-up; log the
  client out on the HIL nodes (`~/cloudtest/xdg`, the AE3's `~/.config/openmv-ota`).

## Hardening (after the functionality is done)

An external audit (and SOC 2, which certifies company controls, not code) is out of
budget for now; these are the free/cheap passes to run first, in order.

- **Internal red-team passes** — `/security-review`, and `/code-review ultra` on the
  signing/verify/installer/anti-rollback path and the camera-identity path (registration,
  signed check-ins, lockboxes).
- **When there is budget** — a targeted pentest of the update + signing chain only.

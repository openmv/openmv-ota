# openmv-ota — what's next

To-do only. Done work is in `git log`; deliberate non-goals are in
[docs/compliance/residual-threats.md](docs/compliance/residual-threats.md); this
file's own history has the longer design notes behind each line.

- **WINC demo wedges after a relay stall** — the H7 Plus demo (2026-10-15, minutes after a relay
  redeploy) logged `live[console]: relay send stalled for 10 s; reconnecting`, then every OTA cycle
  failed with `OSError(12)`: the WINC module itself out of memory (`M2M_ERR_MEM_ALLOC` on DNS), not
  the heap. Two sockets left open with unread data are enough to starve it, but no path that leaves
  one open has turned up: a stalling, refusing or dropping relay and a hosted relay redeploy all
  recovered on the bench. The camera now rebuilds its network stack, then reboots, instead of
  sitting dead. Left: find what held sockets open in the field (a bench H7 Plus is soaking on the
  hosted cloud).
- **Device lockdown** — debug-port and boot protection for the N6, the AE3 and the RT1062
  (residual-threats: planned; not offered on the STM32H7 boards); until then bench/bus
  access is accepted. It is also what makes a camera's
  keys unreadable on the boards that keep them in flash unsealed (the M4, M7, H7, H7 Plus,
  Pure Thermal and AE3; the N6 seals its keys to the chip).

## Before the hosted cloud goes live (required)

The service is pre-launch, so data can be reset when these land. Website parts are in openmv-cloud's `TODO.md`; registrar parts in
openmv-swd-ids'.

- **WINC firmware PRs to openmv** — the H7 and H7 Plus on the hosted cloud run WINC firmware
  that isn't in openmv yet: branch `kwabena/winc_poll`, six commits, to send as small reviewable
  PRs. The big one is async support -- `poll()` and non-blocking stream sockets, which asyncio
  and the openmv_cloud SDK need -- restructured into steps; with it, a real non-blocking `recv`
  (on master `setblocking(False)` is a 0 timeout, which the WINC API reads as "wait forever", and
  `winc_async_request()` never checks its own timeout). The rest are small: SPI transfer errors,
  DNS error codes, a reset clearing the connection state, ETIMEDOUT vs EAGAIN, and an unrelated
  gen_linker fix. Also to fix there: once the module's send buffer is full, `poll()` still reports
  the socket writable while `send()` returns EAGAIN, so asyncio spins through every stall. Once
  they land, drop the installer's WINC `relax()` around each recv.
- **gen_linker PR to openmv** (send with the WINC batch, week of 2026-10-26 or the next) — branch
  `kwabena/gen_linker_board_dir` (2b4458c11, off master, in the shared openmv repo): three lines so
  `tools/gen_linker.py` reads `OMV_BOARD_CONFIG_DIR` like the rest of the build. Without it the OTA
  firmware overlay that relinks for the boot stub produced `.text` at the stock origin with
  `VTOR` at the stub's -- a brick. The OTA tool's N6 stub build needs it in the pegged firmware.
- **Pre-launch cleanup** — revoke the test OTA tokens used for board bring-up; log the
  client out on the HIL nodes (`~/cloudtest/xdg`, the AE3's `~/.config/openmv-ota`).

## Hardening (after the functionality is done)

An external audit (and SOC 2, which certifies company controls, not code) is out of
budget for now; these are the free/cheap passes to run first, in order.

- **Internal red-team passes** — `/security-review`, and `/code-review ultra` on the
  signing/verify/installer/anti-rollback path and the camera-identity path (registration,
  signed check-ins, lockboxes).
- **Left from the 2026-11 security review** (low severity, each needs a design call):
  - Registration at a desk: the camera signs the desk's challenge and nothing else, so the
    device id and exchange key it reports ride unsigned. A rogue USB device at a desk could
    claim another camera's id. Sign the whole report.
  - An issuer certificate cannot be revoked: `issuer replace` drops the old PEM and the old
    certificate stays valid. Re-pinning the account's roots is the only remedy today.
  - `keys/keykeeper.json` is its own anchor once written: the configured pin is not read
    again, so whoever can rewrite that file can seal later releases to a key of theirs.
    Verify the kept chain from the configured pin every time.
  - `OPENMV_OTA_TRUSTED_PROXY_IPS=*` (what the self-hosting page says to set behind a PaaS)
    takes the leftmost `X-Forwarded-For` entry, which a client chooses when the proxy
    appends: the per-IP rate limit is then per header. Document the proxy requirement or
    take the rightmost untrusted hop.
  - A refused check-in signature is audited when its reason changes, so alternating two bad
    signatures for a known camera id writes one audit row a request.
  - `keys/backends.json` names code to load (a `custom` signer's module, a PKCS#11 `.so`):
    building someone else's project runs it. Same trust as `make` in their firmware
    checkout, but it fires at `build romfs`.
- **When there is budget** — a targeted pentest of the update + signing chain only.

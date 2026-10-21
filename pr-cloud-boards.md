## Six boards on the hosted cloud, from the Getting started flow as written

Bring-up of AE3, N6, RT1062, Nicla Vision, Giga and Portenta H7 against the hosted
OpenMV Cloud, run with the website's Getting started commands verbatim. Each problem
those runs hit is fixed here. All six boards flashed with `flash factory`, published,
streamed live video, console and heap telemetry, and took OTA updates with no manual
reset.

### Every board, same commands
- Boards without room for the public CA bundle (Nicla, Giga, Portenta) get a pinned
  set of the hosted cloud's roots scaffolded by `project new` (11 roots, ~17 KB),
  instead of a refusal. Self-hosters replace `certs/root.pem` or pass `--ca`.
- mbedtls speed options (`HAVE_ASM`, `ECP_NIST_OPTIM`, `CURVE25519`) on every board that
  does TLS updates, not just the CA-bundle boards. Handshakes measured at 0.3-0.55 s;
  without them an M7 handshake can outlast Cloudflare's 10-15 s stall limit.
- `project new` stamps `server_url` so firmware-resident recovery has somewhere to go,
  and has a commented `account_id` line.
- `flash factory` works with the camera plugged in over USB on every board:
  - It finds the project's SDK tools. No `--sdk-home` or `--dfu-util` is needed.
  - It runs every precheck before resetting the board.
  - It settles before the first DFU write (the N6 failed on `dfu-util -w`).
  - On RT1062 it goes through the resident bootloader. The SDP/jumper path is now `--provision`.
  - Arduino boards find `-factory-romfs.img`.
  - The 1200-baud touch retries, then asks the user for a double-tap.
  - The bootloader step prints one clean line instead of 40 lines of mpremote traceback.
- `client release publish` works on a plain `pip install openmv-ota`.

### The generated main.py is the Getting started app
- Wi-Fi/LAN variables, a 1 s heartbeat log, a heap-use graph (`datalog` topic `heap`,
  field `used_pct`), live video, and `CHECK_IN_S = 300` passed to `run()`.
- `datalog.enable()` is now called. The old template never called it, so telemetry
  buffered on the camera and never uploaded.
- QVGA instead of VGA: VGA RGB565 overflowed the Nicla's frame buffer.
- Falls back to grayscale on mono sensors (HM01B0/HM0360).
- A crash in the app no longer stops updates. A crashing *unconfirmed* update reboots, so
  it rolls back.
- The website's Getting started page is tested byte-for-byte against `_APP_MAIN_OTA`.

### Runtime
- **Check-in failures.** A failed check-in now retries after 10/20/40… s, capped at the
  check-in interval, instead of a whole poll (an hour). `recover=` fires after 5 failures,
  about 150 s, not about 3 h. On the RT1062, the LAN DHCP swap failed the first check-in
  on every boot.
- **Fresh-heap installs.** An update offered on a periodic check-in now reboots and
  installs from the boot check-in. On the Nicla, a running app's heap made installs fail
  with MemoryError, sometimes before the erase, which left the device stuck on the old
  version forever. Also:
  - The installer uses one receive buffer.
  - It refuses an install that won't fit before erasing anything.
- **Check-ins under memory pressure.** A check-in that runs out of memory asks Live and the
  datalake to let go of their TLS sessions, then retries.
- **Clock.** Each check-in's `Date` header corrects the clock. The fix sits in the romfs
  runtime, so OTA-updated devices get it. Drift of -300 s to +35 s was measured.
- **Logging.** OTA warnings and errors reach the cloud console, even when the logger has a
  handler of its own. The log is flushed before the fresh-heap reboot.
- **Relay keepalive.** Each relay socket sends a `ping` text frame every 30 s; the relay
  auto-answers `pong` without waking. 70 s of silence means the connection is dead, and the
  device reconnects. Without this, a relay redeploy left every camera on a half-open
  socket forever.
- **Import order.** `openmv_cloud` wires into `openmv_ota` after the functions it names.
  A top-level call above its targets raised NameError on device only. A test now walks
  every device module's AST for this.

### Build
- Default delta bases include the last 3 versions built locally, not only the factory
  image. A device a few releases behind now gets a patch instead of the full image.

### HIL
- `recover.py` no longer kills its caller (`pkill -x`). `--reset` no longer probes the
  REPL, which killed the app. On Arduino boards it pulses nRST only: a connect/r/g after
  the pulse read as a double-tap and dropped the board into DFU.

### Not fixed here (firmware)
These are boot-time C-level hangs with no timeout. With the default `openmv_wdt` off,
nothing recovers them:
- The Nicla's boot install spun in `qspi_read_cmd` (`while(!(QUADSPI->SR & TCF))`).
- A Portenta reset during SD activity spun in `sdcard_wait_finished`.

### Tested
- Full suite: 100% coverage, ruff clean.
- All six boards against the hosted cloud, on the bench, across three rounds, including
  a relay redeploy recovered unaided by the keepalive.

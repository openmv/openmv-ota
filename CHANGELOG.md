# Changelog

Notable changes to openmv-ota. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`client device watch`: the Live view from a terminal.** Saves a camera's frames
  (`--out DIR`, or `--latest FILE` kept current), or with `--console` prints its console as it
  runs, each line once across reconnects. It mints its own viewer grant and renews it on every
  reconnect, sends a real User-Agent (Cloudflare blocks library defaults), and is bounded by
  default: one frame, or `--frames`/`--seconds`, and it gives up after `--idle` seconds (60) with
  nothing from the camera. `--json` streams one object per event. `websockets` joins the base
  install for it.
- **Every page's reads on the CLI.** `client dashboard` (every count the overview shows, in one
  read), `client device registration` (a claimed camera's keys, desk and signed check-ins),
  `client device neighbors`, and `client account delete` for the server's root (a deactivated
  account only), so nothing the website shows needs a browser.
- **Times read like times.** Every option that takes a moment (`data series --since/--until`,
  `device list --seen-since/--not-seen-since`, and the new `data logs --until`, which opens the
  console at a moment in the past) also takes `30m`/`2h`/`1d`/`1w` ago or an ISO date in UTC.
- **Device keys on every OpenMV Cam and Arduino camera board.** Each camera holds two P-256 private keys that
  never leave it: an identity key (signs) and an exchange key (ECDH only), behind one interface,
  `openmv_ota.se`. Where they live is board data, and the firmware carries only that board's
  module (frozen as `openmv_se`, so recovery has the keys too; about 3.5 KB):
  - **SE050** (RT1060, Nicla Vision, Portenta H7): NXP's factory identity key and certificate,
    and an exchange key the chip generates, whose policy allows key agreement only.
  - **ATECC608** (Giga R1): configured and locked exactly as Arduino Cloud does it, so the board
    still onboards there; the keys in slots 2 and 3, which Arduino never touches.
  - **Soft** (M4, M7, H7, H7 Plus, Pure Thermal, N6, AE3): the camera's own keys in a 4 KB key
    area at the end of the boot partition -- past the bootloader, untouched by firmware and romfs
    updates. On the AE3 that's MRAM, written with interrupts off; `flash bootloader` blanks it. Sixteen write-once slots, each record committed by a SHA-256 written last, so a
    power cut never silently changes the keys. On the N6 the keys are sealed with AES-256-GCM
    under the chip's hardware unique key, so its external flash alone opens nothing.
  - The build refuses a bootloader that would grow into the key area; `flash bootloader` warns
    that on these boards it erases the camera's keys.
- **Keys are made at a desk, never in the field.** `se.open()` only reads: a camera without
  keys, or with damaged ones, raises. Keys are made by `boot.py` booting a factory image, before
  the app runs (made if missing, never replaced); `flash factory` asks the camera for them in that
  boot's short window, checks a signature over a fresh challenge, and prints both public keys. On
  STM32 boards the window closes after 0.75 s unless a host has the console open, so a boot
  nobody is flashing starts its app on time; a tool that misses it soft-resets the camera with
  the console held open. A camera whose keys fail fails the flash.
- `ecdsa_verify.sign` / `public_key` / `ecdh` on the camera (mbedtls; ECDH is its own
  `mbedtls_ecdh_compute_shared`), and the `key_store` C module (reads the key area, programs
  blank flash in it, and on the N6 seals and unseals with ST's SAES driver).
- [Device keys](docs/reference/device-keys.md): the reference for all of the above.
- **Registration: a camera's keys, signed to its account.** `flash factory` signs a registration
  record (the camera's two public keys, its id and the account) with a **desk key** and uploads
  it; a server can store records but can't make one. `project keys desk create|list|renew|revoke|
  push` manage desk keys -- one per desk, encrypted at rest, expiring after 90 days. New `register`
  token scope for factory stations; `POST /api/v1/admin/registrations`, `PUT|GET
  /api/v1/admin/desk-keys`, `GET /api/v1/admin/devices/{id}/registration`. A changed key is
  audited as `device.rekeyed`.
- **Releases encrypted for each camera, and keykeeper.** With `[ota].per_camera_encryption`,
  a release's key is sealed to **keykeeper** -- a small private service (`openmv-ota keykeeper
  init|serve|chain|pin|rotate`) -- with the account and its desk keys, and each camera is
  offered it in a 145-byte lockbox only its exchange key opens. Keykeeper checks the camera's
  registration against the desk keys in the seal, not the server's word; no firmware holds a
  payload key. Keykeeper's keys rotate on a schedule, each generation signed by the one before,
  and `project keys keykeeper sync` follows them from `[ota].keykeeper_pin`. The update server
  takes `OPENMV_OTA_KEYKEEPER_URL` / `..._TOKEN` and serves the chain at
  `GET /api/v1/keykeeper/chain`.
- **Signed check-ins.** A registered camera with a set clock signs each check-in with its
  identity key (`X-OMV-Signature`). Only a signed check-in gets the full treatment (fleet row,
  telemetry, Live and ingest); an unsigned one (or one signed at a stale time: a clock that
  drifted while the camera was off) gets update offers only, and a bad signature, or one from a
  camera with no registration, gets nothing. Recovery and the M4 check in unsigned.
- **Claiming boards.** Registering a camera claims it: an Arduino board is a registered device
  once its account holds a registration for it, and until then is listed in the account's
  unclaimed boards (`client device unclaimed`). Registering is bounded by board type, the device
  limit and a rate (`OPENMV_OTA_REGISTRATION_RATE_PER_MIN`). `client device release` gives a
  camera up; `flash register` claims one that is already flashed.
- **The hosted keykeeper's pin is built in**, so per-camera encryption on the hosted server needs
  no `[ota].keykeeper_pin`.
- **Keykeeper backups.** With `OPENMV_KEYKEEPER_BACKUP_TOKEN` set, keykeeper serves
  `GET /v1/backup`: `chain.json` and `private/*.pem` as a tar.gz, exactly as they are on its disk
  (the keys still encrypted with the passphrase), the same bytes for the same keys, its SHA-256 in
  `X-Keykeeper-Backup-SHA256`. The backup token opens that route alone, and the update server's
  token doesn't open it; unset, the route answers 404. Keykeeper checks its keys first and answers
  500 with the reason rather than serve a plaintext key or a ring that wouldn't restore.
  `openmv-ota keykeeper restore <archive>` puts a backup back into an empty directory, once its
  chain verifies from its own first generation and every key opens with the passphrase and is the
  key the chain announced.
- **Secure element demos** in `examples/secure_element/`: read the keys and certificate, sign a
  challenge, verify it on the host.

### Removed

- **`OPENMV_OTA_SERVE_UNREGISTERED_BOARDS`.** The server no longer treats every Arduino board as
  registered: one is a registered device only once claimed, by a registration to the account it
  checks in under.

### Fixed

- **A camera turned away at the device limit raises `device.refused` again.** With signed check-ins
  required, the limit turns cameras away when they register, and that was audited as
  `device.register_refused` -- so the dashboard's "turned away" alert and the `device.refused`
  webhook stopped seeing them. A registration refused at the limit is now `device.refused`
  (`"at": "registration"`); `device.register_refused` is for records that don't verify.
- **`flash register` reaches a camera whose app never reads its console, and leaves it running.**
  The key request piled up unread before the soft reset, and on the AE3 (TinyUSB) a full receive
  buffer stopped the port taking input, so the reset never arrived. A running camera is now reset
  first, and `flash factory` sends at most five requests before its fallback reset. And a soft
  reset with the console held open skips `main.py`, so every camera `flash register` touched was
  left at its REPL, never checking in again; a camera the tool soft-reset is now hard-reset once
  it has answered.
- **`flash factory` no longer crashes on a port udev hasn't handed over yet.** A camera back on
  USB after flashing can enumerate before its port is in the dialout group, and the first open
  of its console failed with a traceback (EACCES). It now retries for a few seconds, then fails
  saying why.
- **The keykeeper messages name the right command.** The errors a build gives when a project
  encrypted for each camera hasn't synced keykeeper's keys said `client keykeeper sync`, which
  doesn't exist; they now say `project keys keykeeper sync`.
- **An account's device count is what its limit enforces.** `client account limit` reported the
  fleet alone; it now counts the cameras registered to the account too, as the limit does.
- **The webhook catalogue names the camera-identity events**: `device.registered`,
  `device.rekeyed`, `device.register_refused`, `device.released`, `device.checkin_refused` and
  `desk_key.put` (they already reached `*` and `device.*` subscribers).
- **A half-programmed key word no longer crashes an H7 at boot.** A power cut while the OpenMV
  Cam H7, H7 Plus or Pure Thermal programs its keys can leave a flash word with an error its ECC
  can't correct, and reading it was a bus fault: the camera crashed every time it read its keys.
  `key_store` now reads such a word as EIO, and the key scan treats it like any other write cut
  off before it was committed (skipped) or after (damaged). Reproduced on a Pure Thermal by
  programming one flash word twice.
- **A Wi-Fi radio that fails to start is started again.** `active(True)` hides a failed radio
  start, so the app died on `connect()` with `OSError(EPERM)`. On the Giga this happens when a
  USB host reads the `/flash` drive while the radio's firmware loads, since both come from the
  same QSPI flash. `openmv_ota.wifi()` and recovery now stop the radio and start it again, up to
  three tries.

## [1.0.7] - 2026-10-07

### Fixed

- **A flat heap on the camera.** The cloud SDK allocated constantly, so every camera's heap
  graph climbed to full and dropped back, and on the small-RAM boards (H7, H7 Plus, Nicla)
  the heap ended up so fragmented that Live's TLS connection could not find an 8 KiB block
  with half the heap free -- no video. Measured on an H7, an idle camera went from ~11 KiB/s
  of allocation to ~4 KiB/s, and the heap now holds within ~4% instead of swinging 0-100%:
  - the console and telemetry share ONE keep-alive datalake connection (each opened a new TLS
    session every 5 s -- the datalake's chunked reply made the client drop the socket);
  - requests are written into reused buffers and replies parsed in place;
  - Live frames go out without a copy (the WebSocket header is written in front of the frame
    and both go straight to the socket);
  - `await cam.snapshot()` no longer builds a keyword dict per frame, and a console line is
    formatted once;
  - a small collector gathers garbage once ~2% of the heap has been allocated (a few
    milliseconds each), so what cannot be avoided -- the camera's own image object every
    frame -- never piles up. Tune it with `openmv_cloud.configure(gc_bytes=...)`.
- Live video: a frame taken while nobody was watching stayed "in flight", so the next viewer
  got no frames until the camera restarted. It is released now.

### Added

- Rollout ramps, server side: a ramp's status reports the current stage's progress toward its
  gates (`soak_left_s`, `attempted_left`, its failure rate against the ceiling); raising a ramp
  by hand jumps to the furthest stage the percent reaches and restarts that stage's soak;
  resuming restarts the stage's window; list rows carry `ramp: {stage, of}`; the auto-raise
  never lowers a hand-raised percent; `rollout.autoraise` is a documented webhook event.

## [1.0.6] - 2026-10-07

### Fixed

- **Live video no longer dies silently.** A relay write had no time limit: a large frame
  the network stopped taking parked the video sender in `drain()` forever -- every later
  frame dropped as "in flight", the keepalive and pong writes queued behind it, nothing was
  logged, and the camera stayed "online" sending nothing until a restart (seen on the N6,
  RT1062 and H7 Plus). Every relay write is now bounded (10 s): a stall ends the session with
  `live[0]: relay send stalled for 10 s; reconnecting` in the console, and video resumes on
  its own. The WebSocket upgrade is bounded the same way.
- Server: an out-of-range number in a request (a timestamp past what a datetime holds, an
  integer past a 64-bit column) is a 422, not a 500; every route's error statuses are in the
  OpenAPI schema (checked with Schemathesis in CI).
- Server: the Postgres connection pool stays open (min = max), so a burst of reads after a
  quiet spell no longer waits on new connections -- single reads of 2-6 s.
- The generated app's `openmv_ota.run(...)` line is wrapped (it was 121 characters).

### Documentation

- The datalake tutorial covers sending data from the camera: how `datalog.post()` becomes
  topics, fields, tiles and charts, how labels come from names, and adding a metric.

## [1.0.5] - 2026-10-06

### Fixed

- A device's background flushers (the console's datalake upload, the live console tick, the
  telemetry upload) outlive a cycle that raises. On a full heap one step raised MemoryError
  and ended the task for the rest of the boot: the console went quiet until a reboot.
- Cameras are quieted before every reset the runtime takes (the fresh-heap reboot and the
  installer's reboot into a new release): a camera still streaming through an MCU reset can
  latch its module (the PAG7936) dark until its power is cycled. New seam:
  `register_quiet()` / `quiet_all()`.

## [1.0.4] - 2026-10-06

### Added

- **`GET /admin/dashboard`**: every count an overview shows in one read -- the fleet's
  size and adoption, devices checked in / quiet (over `quiet_hours`), fell back and
  mid-trial, products, active rollouts and those paused for failures, active advisories,
  and devices the plan's limit refused. Each is the number the matching list's filter
  totals, so a dashboard can link straight into that list.

## [1.0.3] - 2026-10-06

### Added

- **`GET /admin/devices/{device_id}/neighbors`**: the devices either side of one in its
  product, in the product's device order (name, then id), with its position and the
  product's device count -- so a device page can step through a product one camera at a
  time without paging the whole list.

## [1.0.2] - 2026-10-05

### Fixed

- A release refused for its account says which side is off. A project that names no
  account gets the exact `account_id = "..."` line to add to `[product]` in
  `openmv-ota.toml`; a real mismatch names both accounts. It used to say only that the
  manifest's account did not match the token.

## [1.0.1] - 2026-10-05

### Changed

- The PyPI page has its own description (`PYPI.md`) written for someone installing the
  package; the repository README stays the contributor's view.
- The package summary describes what ships, not what may come later.

## [1.0.0] - 2026-10-05

### Added

- **Fleet adoption is counted server-side**: `GET /admin/fleet` now reports
  `up_to_date` per product and account-wide, the devices at or past that
  product's newest release. A caller cannot derive this from `by_version`,
  which is keyed by version STRING (`"2.0.0"` against `"10.0.0"` is not a text
  comparison), so a dashboard would otherwise have to page every product and
  compare packed versions itself.
- **`GET /admin/fleet/installs`** (`client installs --days N`): installs and
  failures per UTC day, every day in the window present and zero-filled, oldest
  first. Whether updates are LANDING, which nothing else answered: `/fleet` is
  the fleet as it stands now, and a rollout's counters are one release's story.
- **`GET /admin/activity`** (`client activity`): what has been happening,
  grouped -- the newest event of each (action, actor) with its count. A tail of
  `/audit` cannot answer this: onboarding four hundred devices writes four
  hundred consecutive rows, so the newest N of anything is that one act
  repeated, with everything before it out of reach however deep a caller pages.
- **`installed_on` / `failed_on` on `GET /admin/devices`**: one column of the
  install series as a list of devices.
- **`measured` on `GET /admin/fleet`**, per product and account-wide: the
  devices adoption is measured over. A product with nothing published has no
  newest release to be behind of, so its devices sit outside the ratio rather
  than at 0% of it, and `measured` is the denominator that says so.
- **`totals` on `GET /admin/fleet`** (`client fleet --totals`): the account-wide
  counters alone, with an empty `products`. An overview reads four numbers, and
  an account with thousands of products should not be sent every product's
  version and cohort breakdown to render them.
- **`behind` / `up_to_date` on `GET /admin/devices`** (`--behind` /
  `--up-to-date`): the two halves of the fleet summary's adoption as device
  lists, each device measured against its OWN product's newest release.
  `older_than_release` could not express this -- it takes one release id, and a
  fleet spans products with separate version histories.
- **`seen_since` on `GET /admin/devices`** (`--seen-since` on `client device
  list`): the exact complement of `not_seen_since`, so "checked in since X" is
  one count from the server instead of a subtraction in a caller.

## [1.0.0] - 2026-09-16

The version the code reports moves off the `0.0.0` placeholder. No tag and no
GitHub release were cut: this entry records what the 1.0.0 line is, since the
number is user-visible in the API reference each deployment serves at `/docs`,
in `/healthz`, and in the `generated_by` field of every project lock file.

What that line contains, all of it shipped and covered by the suite (2079 tests,
enforced 100% coverage) and, for the update path, exercised on a nine-board
hardware fleet including the negative cases:

- **The project toolchain** — `project new` scaffolds a product, generates its
  key set, and pins the firmware it builds against; `build romfs`, `build
  firmware` and `build factory-romfs` produce the images; `flash factory`,
  `flash firmware` and `flash romfs` write them.
- **Signing that is not optional** — ECDSA over a key set you hold, with
  encrypted PEM as the floor and PKCS#11, AWS, GCP and Azure KMS backends for
  keys that never touch the build machine.
- **A device that comes back** — A/B slots, a trial an update must confirm,
  anti-rollback, and firmware-resident recovery for when no slot will boot.
- **The update server** — releases, cohorts, staged rollouts with auto-pause on
  failures, pins, delta artifacts built against what the fleet is running, a
  hash-chained audit log, and scoped tokens; every list endpoint sorts, filters
  and pages server-side and returns a filter-aware total.
- **Integrations** — capability grants for the live relay and the datalake, so
  a viewer or an ingesting device gets a short-lived token and nothing more.
- **Evidence** — CycloneDX SBOM per release with osv-scanner in CI, and the
  CRA / RED 3.3 alignment tables audited against the regulation's published text.

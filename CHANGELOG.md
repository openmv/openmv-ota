# Changelog

Notable changes to openmv-ota. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Device keys on every board but the AE3.** Each camera holds two P-256 private keys that
  never leave it: an identity key (signs) and an exchange key (ECDH only), behind one interface,
  `openmv_ota.se`. Where they live is board data, and the romfs ships only that board's module:
  - **SE050** (RT1060, Nicla Vision, Portenta H7): NXP's factory identity key and certificate,
    and an exchange key the chip generates, whose policy allows key agreement only.
  - **ATECC608** (Giga R1): configured and locked exactly as Arduino Cloud does it, so the board
    still onboards there; the keys in slots 2 and 3, which Arduino never touches.
  - **Soft** (M4, M7, H7, H7 Plus, Pure Thermal, N6): the camera's own keys in a 4 KB key area
    at the end of the boot partition -- past the bootloader, untouched by firmware and romfs
    updates. Sixteen write-once slots, each record committed by a SHA-256 written last, so a
    power cut never silently changes the keys. On the N6 the keys are sealed with AES-256-GCM
    under the chip's hardware unique key, so its external flash alone opens nothing.
  - The build refuses a bootloader that would grow into the key area; `flash bootloader` warns
    that on these boards it erases the camera's keys.
- **Keys are made at a desk, never in the field.** `se.open()` only reads: a camera without
  keys, or with damaged ones, raises. Every `flash factory` / `firmware` / `romfs` ends by
  provisioning the camera's keys (made if missing, never replaced), checking a signature over a
  fresh challenge, and printing both public keys; a camera whose keys fail fails the flash.
- `ecdsa_verify.sign` / `public_key` / `ecdh` on the camera (mbedtls; ECDH is its own
  `mbedtls_ecdh_compute_shared`), and the `key_store` C module (reads the key area, programs
  blank flash in it, and on the N6 seals and unseals with ST's SAES driver).
- [Device keys](docs/reference/device-keys.md): the reference for all of the above.

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

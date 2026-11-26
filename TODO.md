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
  - The hosted server runs with `OPENMV_OTA_TRUSTED_PROXY_IPS=*`, which takes the leftmost
    `X-Forwarded-For` entry. `OPENMV_OTA_TRUSTED_PROXY_HOPS` takes the entry the outermost
    proxy wrote; the hosted deployment's hop count has to be measured before it is set.
  - A refused check-in signature is audited when its reason changes, so alternating two bad
    signatures for a known camera id writes one audit row a request.
  - A camera names its own `product_id` on every check-in, and the fleet row follows it. On
    an account that models each customer as a product, a customer's camera can report a
    sibling product: it is offered that product's release, shows in its fleet, and a
    fallback from it counts against that rollout. Refusing it was tried and withdrawn (it
    stranded cameras making legitimate moves); a change the account did not send the camera
    to is the `device.product_mismatch` audit event, and that is where this stands.
  - A product-limited token lists every cohort label in the account, and assigning by
    product declares a label account-wide.
  - Whoever answers the check-in can reboot a camera once a poll: an offer after the boot
    check-in makes it reboot to install from a fresh heap, before the manifest is fetched
    and vetted. Vet the manifest in place first, so only a signed, newer release costs a
    reboot.
  - On a stub board whose trial slot spent the stub's own budget and whose other slot does
    not verify, the stub jumps by its record and `boot.py` mounts the spent trial on every
    boot, never reaching recovery. `boot.py` should read the stub journal's no-slot reason.
  - Recovery on a stub board sends no stub version, so it could never take a release with
    `min_stub` set. Nothing sets `min_stub` today.
  - The publish counter a platform build signs comes from the server and is only bounded
    (below 2**48). Record it in the project ledger and refuse a jump.
  - Keys minted before 2026-11 keep their 2048-round encryption until rewritten: add a
    `keys rewrap`.
  - The signed check-in replay cache is per process: with several workers, or across a
    restart, a capture replays once inside its five-minute window.
  - The server reads what its own upstreams send without a size cap (the advisory service,
    the registrar, keykeeper, the datalake), and on local storage a download reads the
    whole artifact into memory per request.
  - A publisher's upload is held in memory whole: about 1.7 GiB a request at the default
    caps, with no cap on how many run at once.
  - A webhook endpoint that sends its status line a byte at a time holds the one delivery
    worker for as long as it keeps sending.
  - `flash register`: a malformed identity line from the USB device is a traceback, and its
    error text is printed with control characters.
  - The cloud SDK's `poll_watch` and `_ws_connect` read header lines with an unbounded
    asyncio `readline()`.
  - A `manage` token can admin-bind a camera id nobody registered; the owner's registration
    is then a 404 and no API call undoes it.
  - One sequential webhook worker over a global queue: a tenant with many endpoints that
    answer just inside the timeout delays every other account's deliveries.
  - `OPENMV4`, `OPENMV4P`, `OPENMVPT` and `H7` are one row at the registrar, so a tenant
    can register another's camera id under a sibling board name and hold a seat on it.
  - A desk key's expiry comes from the request that stores it, so a `manage` token can
    extend one (not un-revoke it).
  - An anonymous client naming an account can push real boards out of that account's
    capped unclaimed list with made-up ids.
  - A webhook ping's audit row lands in the operator's account, not the caller's.
  - `client data export` to stdout, error details and a few listings print server and
    camera text unfiltered; `--json` output is safe.
  - `project keys restore` overwrites the keys in place with whatever a well-named archive
    holds, without checking that they open.
  - A `keys/keykeeper.json` written before pins carried the record hash, holding a pin of
    generation 2 or later, is refused with an error that does not say to delete it.
  - Stub: `verify.c` never reads the certificate's `kind`; nothing calls `port_wdt_start`
    although the design says the stub arms the trailer's watchdog; a failed install runs
    one boot on the RAM copy of an erased record; a signed trailer with `comp_count == 0`
    scrubs the firmware area on the N6 and RT1062; boards without slot encryption do not
    check `body_sha256` in the stub.
  - Round 6 (server): a device pin on an
    id that is learned-bound to another account hides the camera from its owner until it
    registers; a learned binding from an unsigned check-in takes any account string.
  - Round 6 (host): `keys retire` re-certifies every entry of `trusted_keys.json` without
    checking that its existing certificate verifies under the root; the keykeeper checks a
    registration's signature but not its freshness, and seals for expired desks; USB serial
    strings in `flash list`, the camera's `SE-ERROR` text and server `detail` in errors
    print unfiltered; `flash bootloader -b OPENMV_AE3` runs the project's
    `tools/alif/toolkit/*.py`, and a committed `openmv-ota.local.toml` picks the SDK home
    whose `dfu-util` runs; the SDK archive's `.sha256` comes from the same host as the
    archive.
  - Round 6 (device): an unverified offer with a new `release_id` reboots the camera before
    anything is fetched (a hostile server can loop it); the factory window signs any 32
    bytes with the identity key (console access); a `.hilcov_uart` file on a volume turns
    on debug logging; the installer's header reader keeps up to 64 headers of 4 KiB.
  - Round 7 (device, host): the firmware's frozen `_boot.py` runs before `boot.py` with the
    partition's first ROMFS mounted and on the import path. The installer no longer leaves
    an unverified one there, but a signed image that is retired or below the rollback floor
    and ships its own `os.py` still runs that file early; closing it needs an OTA-owned
    `_boot` prelude. On the WINC the download's receive blocks, so neither the 30-second
    timeout nor the rate floor counts there. `certs/ca.pem` as a link to the system
    bundle, or `[ota].ca` outside the project, is refused; a `device/<module>` that is a
    directory is a bare error.
  - Round 21 (host): the camera's JSON caps bound values, not heap. On the M4 (OPENMV2,
    a 44 KB GC heap) a hostile check-in answer inside the caps (about 455 strings of 16
    characters, some 24 KB parsed) runs the heap out and the runtime reboots, and the
    installer holds three copies of a manifest of up to 8 KB (raw, region, body) before it
    parses one: give each value a byte budget, and parse from memoryviews. `poll_watch` can
    still raise `MemoryError` from `_read_capped`. In the app, a TLS failure on a clock NTP
    set does not end the six-hour hold on NTP (Round 15's "minutes to six hours"): a failed
    check-in does not say whether TLS or the network failed, and dropping the hold on every
    one would put NTP on the wire every five minutes of an outage. The ignored files a build
    leaves in the firmware tree (`build/`, mpy-cross's `build/`, `__pycache__/`) are not looked
    into: a tree handed over with planted objects (`--incremental`) or a byte-code cache whose
    source's mtime and size it matches runs them, as Round 18's mpy-cross does. Two hosts in
    separate PID namespaces writing one download name in a shared directory can collide on
    its temporary name.
  - Round 19 (host): Windows is not a supported host (CI runs Linux and macOS): there,
    `CreateProcess` and `shutil.which` look in the working directory before `PATH`, so a
    `git.exe` or `dfu-util.exe` at a project's top runs; every tool wants resolving to an
    absolute path outside the project before Windows is. On a board that does not verify TLS
    no server time is verified, so an image stamped by a build machine whose clock ran fast
    holds NTP off until a release built with the right time.
  - Round 18 (host): a firmware tree handed over as a directory (an archive, a copy) brings
    its untracked `mpy-cross` with it, and a build runs it; trusting only a binary this tool
    built wants its hash kept somewhere the tree cannot write. A hard link from a package
    cache (`uv pip install --target app/lib`) is refused like any other. The check of the
    working directory looks at that directory, not at a project above it.
  - Round 17 (host): a FIFO in `app/` passes the project check; `build` as a file, and a
    directory where a file is written, end in a traceback; on Windows a release's file name
    taken from its URL, and a `C:name` entry in `romfs unpack`, are not contained (not
    tested there).
  - Round 16 (host): a project with `platform = true` has `build ota-romfs` take a publish
    counter from the server the user is logged in to; `flash bootloader --dry-run` on the
    AE3 writes its key-area file.
  - Round 15 (device, host): NTP cannot rewind the clock past the newest time a verified
    server gave since boot, or, after a reboot, past when the image it runs was built (the
    trailer's `built`). Between a release and a reboot that floor is as old as the release.
    Forward, the clock is anyone's on the
    path: a spoofed NTP answer, or an unverified `Date` on a board that does not verify
    TLS, makes check-ins stale at the server until NTP or a verified server corrects it
    (minutes to six hours, never where NTP is blocked and the server unreachable).
    Host: `--keys-from` follows links in the source's key files; the ROMFS reader
    copies a slice of the image for each record that points into it; `stage_app` copies a
    FIFO or a device; the Azure signer talks to whatever host a record's `uri` names (the
    SDK checks the challenge from 4.7, now the floor).
  - Round 14 (host): `flash register --token` sends the
    token it is given to the server the project names (only the saved one is held back);
    `project setup` reuses a cached checkout without checking its HEAD is the locked commit (the build's
    drift check catches it); a tracked file name that is not UTF-8, and a lock or manifest
    field of the wrong type, end in a traceback.
  - Round 12 (device, host), each needs a bench: the STM32 stub reads the four places of
    the flash key in one call, so a word torn in the first (power cut while it was
    programmed) fails the read of all four; the RT1062 fuse key's plausibility is checked
    on the words drawn, not on the fuses, before the lock; the N6 and RT1062 stubs leave the
    derived key in RAM when they lock the engine (the H7 and AE3 ports clear it). Host:
    `gzip.decompress` of an image or a component has no size cap; a ledger entry's `path`
    is joined without containment.
  - Round 9 (device, host): a signed image that ships
    a module named as a firmware one (`openmv_se`, `logging`) is imported ahead of it. The
    connect and the TLS handshake block: a peer that drips its handshake holds the
    installer, and every check-in, for as long as each wait stays under the socket timeout,
    or until the watchdog's minute where one is armed (nothing in Python can end a blocking
    handshake, and the WINC cannot do a non-blocking one). `openmv-ota project history` prints a
    file's fields as they are. The N6 stub's flash
    reads and writes have no range checks of their own, a failed re-map leaves the part in
    octal mode, and the prescaler rounds down; the H7 stub retries a dead QSPI part on
    every call. A resumed download's `Content-Range` is not compared with what was asked.
    The recovery check-in's body is read an object a read. A firmware tree's version
    numbers are not range-checked.
  - Round 8 (host): `project new --force --new-keys` overwrites the key backup with the
    new keys'. `flash factory` takes `SE-NONE` from a
    board that declares a secure element.
  - Round 7 (server): "is it news" checks are read-then-write, so a burst can audit a few rows
    instead of one; registration's device-limit check is check-then-write; an unsigned
    check-in naming a registered camera with a sealed release pending is one keykeeper
    call; a token in account `''` reads every account's `/fleet/bases`.
  - Rounds 8-10 (server): journal entries the audit budget dropped are not audited later; a
    `stale` refusal uses the day's refusal-audit slot.
  - Round 19 (server): an `accounts` token's customers are filed under its name (`_owner`),
    which is unique only within its account, so two such tokens with one name share one
    customer directory (only the server's root mints them). A forgotten camera's ingest
    token stays valid for up to a day, so its data can arrive again after the erasure. The
    auto-pause reach still counts a camera mid-trial: the fleet row does not keep the slots
    the offer's `settled` reads.
  - Round 20 (server): after a limited token's forget, the whole-account token's forget of
    the same id answers 404 (the fleet row is gone), so what the camera stored under its other
    products waits for retention unless it checks in again. A publish token's storage has no
    per-account cap (an image and 32 deltas of up to 512 MiB each per publish, any count). A
    create that loses the cohort twice (409) has already audited and webhooked `superseded`
    naming a rollout that never existed; a PATCH has no stage guard beside an auto-raise; an
    old instance's create landing between v46's supersede and its index fails that deploy's
    migration (the next start heals it).
  - Round 18 (server): a rollout's status (`rollout_status.on_release`, `_ROLLOUT_STATE`)
    still orders a camera by payload version alone, so a camera built with `PRODUCT_ID` 0 can
    read as behind or ahead on the dashboard (display only; the offer and the auto-pause
    order it by the publish counter). A datalake record holding NaN or Infinity stored before
    ingest refused them still makes its reads 500; nothing cleans one out. The datalake's
    `device_stats` table, unread since device reads went by product
    (`device_product_stats`), goes in its next change; objects it filed before it knew their
    product (before 2026-09-14) are read by no route and wait for retention.
  - Round 17 (server): the hosted server still takes the leftmost `X-Forwarded-For` entry
    (`OPENMV_OTA_TRUSTED_PROXY_HOPS` unset until the platform's hop count is measured), so a
    client names the address it is rate-limited as. The datalake's arrival indexes do not
    lead with the account: a new owner reading back past a camera's transfer scans the old
    owner's rows for that topic (slow, not a leak).
  - Rounds 13-16 (server): a product a camera was sent to by a pin, and never went to,
    stays noted on its registration, so a later move there by itself reads as sent. A
    webhook test ping has no
    rate limit and its audit row lands in the unnamed account. An account can pin a device
    id it has never seen, and the id's later owner cannot pin or unpin until the camera
    checks in. Two accounts registering one id at once, or one account registering past
    its device limit at once, are read-then-write.
    Also: the CLI's token and account commands write no audit row; the stored capability
    secret wins over a changed environment value; a scan has no cap on findings and reads
    one page of OSV results; path and query `product_id`s are unbounded; the audit hash
    joins fields with `|` unescaped and leaves the account out; `list_products` aggregates
    the whole account to answer one product; a datalake answer that is not a JSON object is
    a 500; migrations are not serialised across instances starting together.
- **When there is budget** — a targeted pentest of the update + signing chain only.

# Changelog

Notable changes to openmv-ota. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`project keys retire`: a revoked key is refused by the fleet.** Cameras have checked a
  certificate epoch since trailer v4, but no command ever raised it, so `keys revoke` only
  stopped the project signing with a key. `keys retire` has the root re-issue the certificate
  of every key that is not revoked at the next epoch; a camera that confirms a release signed
  after that refuses the revoked keys, and every image signed before, for good. `keys status`
  shows the epoch and any revoked key cameras still accept; a retired key cannot be unrevoked;
  keys provisioned into an external backend are certified at the project's current epoch.
- **`project keys add`: the key pool grows.** The pool was fixed at `project new`. `keys add
  --ota-keys N --factory-keys N` mints more keys, certified by root 0 at the project's
  current epoch, as encrypted PEMs or inside an external backend (`--backend`). Cameras embed
  only the roots, so they accept the new keys with no firmware change.
- **The AE3's app is ciphertext at rest.** On the Alif Ensemble E3 the two ROMFS slots live on
  an OSPI flash part beside the chip, and the booted slot is now stored encrypted there,
  deciphered by the OSPI controller's AES engine as it is read. The key is the camera's own:
  sixteen random bytes made on its first boot and kept in the chip's MRAM, with a key per slot
  image derived from it. The boot stub and the installer cipher what they write in software;
  the first boot after a factory flash makes the key, resets, and ciphers the factory slot
  (under a minute, once). The firmware itself is in the chip's MRAM and stays in the clear.
  The cipher is AES-128 one block at a time, which is what the engine does: equal blocks of
  one image are equal in flash. See `docs/design/flash-encryption.md`, "The AE3, as built".

- **The H7 Plus's and the Pure Thermal's app is ciphertext at rest.** These cameras keep their
  two ROMFS slots on a QSPI flash part beside the chip, and the slots are now stored
  encrypted there. The STM32H743 has no engine to decipher flash as it reads it, so the
  firmware deciphers the booted slot once at boot into RAM (a block of the frame-buffer pool
  as big as the app image) and mounts `/rom` there; the boot stub and the installer cipher
  what they write. The cipher is AES-128 in counter mode, in software. The key is the
  camera's own: sixteen random bytes made on its first boot and kept in the chip's flash; the
  first boot after a factory flash makes the key, resets, and ciphers the factory slot (under
  a minute, once). The firmware itself is in the chip's flash and stays in the clear. The
  Arduino boards are not part of this: their frame-buffer pool is too small for the copy.
  See `docs/design/flash-encryption.md`, "The H7 Plus and the Pure Thermal, as built".

- **The RT1062's flash key is its own, in fuses it burns once.** The first boot of the boot
  stub on an RT1062 whose SW_GP2 fuses are not locked takes 128 bits from the chip's random
  number generator, burns them into SW_GP2 and burns its write lock and read lock. From then
  on the flash key is derived in the chip from those fuses on every boot; software reads
  0xBADABADA there, on an open chip as on a closed one, and the key never leaves the chip.
  **This is permanent and automatic: flashing an OTA firmware onto an RT1062 uses up its
  SW_GP2 fuses.** No other fuse is written. Before this the key was derived from NXP's
  master key, which an open chip replaces with zeros, so every open camera had the same,
  computable key; a camera never runs on that key now. An RT1062 flashed with an earlier
  build is flashed once more (`flash factory`): what it stored was under the old key.

- **`flash read`: a partition read back as the flash part stores it (RT1062).**
  `openmv-ota flash read -b OPENMV_RT1060 --part firmware|romfs --to FILE` copies the firmware
  area or both app slots out through the resident bootloader, which runs with the chip's flash
  cipher off, and writes nothing. It is the RT1062's counterpart of a DFU upload, and the
  fleet now uses it to check from outside the chip, after every `delta`, that the RT1062's
  flash holds ciphertext, as it already did for the N6 and the AE3.

- **The RT1062's firmware and app are ciphertext at rest.** The i.MX RT1062 joins the N6: its
  boot stub keeps the firmware area and the booted slot encrypted, deciphered by the chip's bus
  encryption engine as they are read; the stub and the installer encrypt what they write, and
  the first boot after a factory flash ciphers the factory slot (about two minutes, once).
  The key is the camera's own: see the entry above.
  See `docs/design/flash-encryption.md`, "The RT1062, as built".

- **On a chip with a secure boot key, the boot stub boots only signed firmware.** With no
  slot that verifies, the stub used to fall back to whatever its install record named, or
  to anything at the firmware address that looked like a vector table -- firmware a USB
  download could have put there. It still does on an open chip. Where the port reports a
  secure boot key (the N6: fuse words 160-167 or the `secure_boot` bits of word 18) it
  stops instead, with event `unsigned-refused` in the boot journal; a signed factory image
  over USB is the way back. `[ota].secure_boot = "always"` builds that stub for any board,
  and the fleet's new `no_slot_secure` leg runs it on the N6.

- **The formats that leave the building are pinned.** `tests/ota/format_pins.json` holds a
  sample of each format that crosses between the build, the server and a camera -- the image
  trailer and its certificate, the update manifest, an encrypted artifact, a lockbox, a
  delta patch, a registration record, the slot status sector, a boot journal entry -- and
  `tests/ota/test_format_pins.py` fails when today's code stops producing or reading one.
  A server test does the same for the check-in: a body with fields the server has never
  heard of is served.

- **The boot stub keeps a journal, and the server hears it.** What the stub decides at boot
  -- a slot refused and why, the firmware area repaired from the other slot, a flash chip
  that needed a second reset -- used to reach its UART and nothing else. The stub now
  appends a 16-byte entry to the second half of its install record's block on every boot
  that was not an ordinary one. The firmware sends the newest entries with its check-in
  (`stub.journal`) and the server writes one `device.boot` audit event for each entry it has
  not seen, which webhooks can subscribe to. A slot installed because a newer one verified
  but would not install is now recorded and logged as `repaired`. Format:
  `docs/design/firmware-updates.md`, "Boot journal".
- **The boot stub carries a signature.** `build firmware` and `build stub` now write
  `<board>-stub.bin` as the stub's whole sector: the code, 0xFF padding and a 512-byte footer
  (`openmv_ota.ota.stub_footer`) that names the code's SHA-256, the stub version and the
  product id, with the factory key's certificate and its signature over them -- the trailer's
  two-step check, for the bootloader that will verify the stub under the project's root. The
  stub never reads it. `build inspect` decodes a sealed stub and `build verify` checks it;
  `build stub` takes `--factory-key`, and both builds take `--allow-dev-key` for a `--dev`
  project's throwaway key. Format: `docs/design/firmware-updates.md`, "Stub image footer".
- **The N6's ROMFS slots are ciphertext in its flash chip too.** The boot stub now keys the
  chip's memory cipher engine over the slot it boots as well as over the firmware area, so
  the app and the compressed firmware the slots carry are stored encrypted. The other slot is
  never mapped: the installer writes it as ciphertext itself, through a `flash_crypt` C module
  the firmware build adds on such a board (the same cipher the stub programs with, keyed by
  the chip), and the manifest names the two inputs it needs before the first body byte
  arrives (`trailer_sha256`, `crypt_size`). A factory image is written in the clear and
  converted on the first boot: the stub copies the slot into the other one through the
  cipher and erases the clear one; a slot in the clear beside a ciphered one that verifies
  is erased, never booted. The fleet's at-rest read-back covers the slots.
- **The N6's firmware is ciphertext in its flash chip.** The OpenMV N6 runs its firmware from
  an external flash that can be read off the board. The boot stub now derives a key from the
  chip's own hardware key (nothing is burned, nothing is provisioned, the key is stored
  nowhere), has the STM32N6's memory cipher engine decipher the firmware area as the core
  runs from it, and encrypts the firmware as it installs it; the engine is locked before the
  firmware starts. Firmware written over USB is in the clear until the first boot, when the
  stub installs the slot's copy encrypted, and an install erases whatever else the area held.
  Until the device is closed a debug probe can derive the same key
  ([encryption at rest](docs/design/flash-encryption.md)).
- **The boot stub proves an install before it runs it.** After an install the stub hashes
  what it wrote through the memory map, the way the firmware will read it, and tries the
  other slot when it does not match (`stub: installed area verified` on the debug UART).
- **`se.seal()` / `se.unseal()`: data only this camera can open.** An application that keeps
  something secret on the camera -- a record on `/flash`, which any PC can mount, a token, a
  setting -- seals it instead of encrypting it under its serial number. The key is derived from
  the camera's exchange key (inside the chip on a board with a secure element, sealed under the
  N6's hardware key), so nothing is stored or provisioned; `aad` names the purpose, and an
  altered blob raises `ValueError` before anything is decrypted. The lockbox's construction
  (AES-256-CBC + HMAC-SHA256, 49 bytes plus padding). `examples/secure_element/seal_record.py`;
  the HIL bench app seals and opens a record on every wifi/lan board each cycle.

- **Desk keys are trusted through a chain, not through the token that pushed them.** Every
  project gets an **issuer key** at `project new`, certified once by root 0 (certificate kind
  `ISSUER`); it certifies each desk key as it is made (kind `DESK`), and the entry carries both
  certificates. The server pins each account to its project's roots (`PUT /admin/roots`,
  `project keys roots push`: set once, replaced only under a statement a current root signs,
  `--rotation-key`) and records a desk key (`PUT /admin/desk-keys/{id}`, now with `cert` +
  `issuer_cert`) only through the chain root → issuer → desk to those roots, with the key's id
  checked against its key. An admin token alone can no longer introduce a desk or register
  cameras. Also `project keys issuer create|replace|show`, `project keys desk certify` (desks
  from before the issuer), `project keys roots show`, `GET /admin/roots`; the issuer moves into
  an external backend with the signing keys (`project keys backend provision`) and is copied by
  `--keys-from`. A camera's parser rejects both new kinds, so no stub or firmware changes. The
  HIL harness pins its bench server and certifies its desk on every run.

- **The boot stub on the STM32H7 boards.** The OpenMV Cam H7 Plus and Pure Thermal and the
  Arduino Portenta H7, Giga and Nicla Vision get the same firmware-in-the-image update the N6
  has: the stub takes the 128 KiB sector the bootloader jumps to (`0x08040000`), the firmware
  is relinked one sector up (`0x08060000`, 1660 KiB) and installed from the ROMFS slot a
  32-byte flash word at a time, the install record sits in the last 4 KiB of the last sector.
  The sector below the stub (`0x08020000`, the internal filesystem's, unused since these
  boards keep their filesystem on the QSPI part) becomes the firmware's **second text region**:
  `build firmware` reads the linker map of the firmware's own link, packs whole objects into
  the 128 KiB biggest-first and re-runs that link with a `FLASH_TEXT2` region, then cuts the
  ELF into `<board>-firmware.bin` and `<board>-firmware-text2.bin`; the image carries both as
  components and `flash` writes both. 1660 + 128 KiB is 4 KiB under the stock window, so no
  board drops an image feature to fit. The stub reads its slots off the QSPI part in indirect
  mode (1-line commands; 4-byte addressing on the 32 MiB part) and hands the controller back
  reset, as both bootloaders leave it; a flash word torn by a power cut reads as an ECC fault
  and the stub reinstalls. The board table's stub layout gains `text2` (`dest`, `max`,
  optional `prefer`/`exclude` object patterns).
- **HIL: the boot stub's own log is captured and scored.** A stub board's project has
  `openmv_log` enabled on the marker UART (the stub reads its UART from the trailer, which
  the build fills from the module constants, not from the bench file the firmware reads), the
  stub's lines are coverage markers (`stub.*`: the verified golden boot, the paid trial, the
  install and its record, the reverted boot; every "gave up" line forbidden), and the
  coverage catalog maps them to the stub's C sources.

### Security

- **A build does not run an `mpy-cross` the firmware checkout came with.** `build romfs`
  prefers the tree's own compiler, and a fork that commits a program at that path had it
  run. The one this machine built is untracked; a tracked one, or a link, is passed over
  for the installed `mpy-cross`.
- **`SDK_VERSION` is a version, and is not quoted back when it is not one.** As a link it
  was any file of the user's, printed by `project status` and every drift error.
- **A project file with another name elsewhere (a hard link) is refused** like a link: the
  lock, the ledger and the build outputs are written in place.
- **A command run inside a project checks it,** whether or not it takes one: `client
  release manifest` and `artifact` write where they are run.
- **An app version is ASCII digits and dots and nothing around them.** A trailing newline,
  a space or another script's digits went into file names and the ledger as they were.

- **Every command that names a project checks it for links first.** The check lived where
  the config is loaded, and the desk, issuer and roots commands never load it: they wrote
  `keys/issuer.json` and `keys/desk_keys.json` through a link. The directories `project new`
  scaffolds into (`app/`, `device/`, `certs/`, `compliance/`) hold no link either.
- **`project new --force --keys-from` does not replace a project's keys** without
  `--new-keys`, a project with keys is not rewritten as a non-OTA one, and `--new-keys`
  without `--ota` is an error.
- **A release a camera would refuse is refused where it is built:** the manifest body's
  bracket and element cap is checked when it is packed.
- **Reading a firmware tree's macros takes no longer for a long line.** Two patterns were
  quadratic in a line of spaces, at `project status`.
- **Connecting is free of the download's clock for five minutes an install,** not five a
  retry.

- **A rollout's failure rate is judged over at least five attempts,** or over every camera
  the rollout can reach where that is fewer. At one failure in one attempt, a single camera
  paused a rollout by itself.
- **A rollout a newer one superseded cannot be resumed.** Resumed, there were two active
  for one product and cohort, and the older release was offered again whenever the newer
  paused. Going back to a release is a new rollout for it.
- **`OPENMV_OTA_TRUSTED_PROXY_HOPS`: the client's address is the one the outermost proxy
  wrote.** Behind a platform whose proxy addresses cannot be named, the server trusted
  `X-Forwarded-For` from anyone and took its leftmost entry, which is the client's own when
  the proxy appends: a new address a request, and no rate limit. Set to the number of
  proxies in front of the server, the entry that many from the right is used.
- **A pin lifted before the camera arrives still sent it** (migration v45). A flow that
  pins a camera to another product's release and unpins it before the camera's next signed
  check-in left its arrival reading as a move of its own. The product an account sent a
  camera to is kept on its registration until it checks in from there.

- **An image the hash refuses never becomes a filesystem.** The firmware mounts whatever
  ROMFS starts the partition before any of this project's code runs, with `/rom` on the
  import path, and its own frozen `_boot.py` then imports `os`, a module a file can stand
  in for. Slot A (and the one slot of a single-image board) starts the partition, and the
  installer programs an image as it arrives, before its hash can refuse it. A stream that
  was not the signed image, left on flash by a failed or interrupted install, was mounted
  on the next boot and its `os.py` ran ahead of every signature check. The installer now
  holds back the image's first 32 bytes (the ROMFS magic is in them; a whole flash word
  on the H7's internal flash, where a word is programmed once) and writes them only after
  the whole image is on flash, read back and hashed.
- **The rollback floors are back on flash before the download is opened.** On a
  single-image board the erase destroys the only copy of the anti-rollback floor and the
  certificate-epoch floor, and they were carried into the slot only once the download was
  open. Someone on the path who refused that connection three times reset the camera with
  neither, and recovery then accepted any old signed release, or one under a retired key.
  They are written straight after the erase.
- **The boot stub's log ring checks its length.** The ring survives a reset in RAM the app
  can write; a length past the buffer made the stub write there before any slot was
  verified.
- **A download slower than 1 KB/s is dropped.** The installer's reader timed out only when a
  connection went silent for 30 seconds, so a server that dripped a byte at a time held the
  installer, and stopped the app, for as long as it kept sending. From the moment its
  download opens, an install attempt now gets a minute, plus a millisecond for every byte
  of the image it has written, never more than two minutes ahead of the clock; past that
  the download fails like a dead link and is not reconnected. It is one allowance however
  many times the download reconnects, and bytes that write no image (headers, chunk
  framing, a stream that inflates to nothing) buy none of it.
- **A check-in answer that nests too deep, or lists too much, is refused before it is
  parsed.** Eight KiB of open brackets, or of short list elements, is hundreds of KiB of
  heap to the JSON parser, and running out there read as the network stack's own
  exhaustion, which ends in a reboot. Recovery refuses the same answers.
- **A manifest body made of brackets or elements is refused before it is parsed.** It is
  parsed ahead of its signatures, behind only a checksum, and the parser builds an object
  for each one.
- **A short image file is refused before the erase.**
- **A stream that ends short is refused before its last bytes are written.** They were
  programmed first, an odd count that is not a whole flash write unit.
- **The manifest is read into whole buffers.** A server sending it a byte a read made the
  installer hold an object per byte.
- **The N6 boot stub tries a failed flash init again.** One bad reset poll or id read was
  remembered as a working driver, after the bootloader's memory map had been dropped.
- **The AE3 builds against the Alif DFP v2.** The firmware moved to it (`lib/alif-dfp`,
  v2.2.1): the boot stub's include paths and device header, and the two firmware modules
  that named the old headers, follow.
- **Host tools read a project's files and a firmware tree without running or following
  them.** `git` is run with the tree's own monitor, hooks and content filters off, wherever
  its config keeps a filter (an included file, a worktree's config, a submodule's); a tree
  whose config has git work on another tree, or whose submodule is a link, is refused, and
  git fetches nothing to read a tree (a partial clone's config names the program that
  would). A project with a link where the tools write or read back is refused by every
  command: a file at its top level (the lock), anything in `build/` or `.openmv-ota/`, a
  file in `keys/`, an output directory named inside it, and `app/`, `app-coprocessor/`,
  `device/` or `certs/` itself (each is packed whole). `project setup` takes the lock's
  commit as a hash before it names the cache directory. A history file with a line that is
  not an event still reads.
- **What a machine loads to sign is that machine's to say.** `keys/backends.json` comes
  with the project, and named the PKCS#11 library to load and the custom signer's
  `module:callable` to import: building a project someone else made ran their code. Both
  now come from the environment (`OPENMV_OTA_PKCS11_MODULE`, `OPENMV_OTA_SIGNER_FACTORY`),
  and a record that names either is refused.
- **`project new --force` keeps a project's signing keys.** It replaced the roots, the
  signing keys and the payload keys of a project that had them, after which a fleet in the
  field can no longer be updated. Replacing them is `--new-keys`.
- **A project's `keys/` is not a link.** As one, it was another project's keys: signed
  with, and rewritten by `keys rotate`. `project new --force` and `keys backup`/`restore`
  check the project for links like every other command.
- **On a board that does not verify TLS, the server's `Date` only moves the clock
  forward.** It is anyone's on the path there: rewound, a retired certificate was valid
  again. It is no longer taken as a network check either, so NTP still runs.
- **The connect and the TLS handshake are not charged to the download's allowance,** up to
  five minutes of them an install. A slow board behind a redirect spent its first minute
  there.
- **`flash register` does not send the saved token to a server the project names.** The
  project's `server_url` came ahead of the saved profile's while the token came from the
  profile; when they differ, `--server` or `--token` says which is meant.

- **A payload version is 32 bits.** A major up to 255, not 31 bits: check-ins accept it and
  Postgres stores it.
- **A check-in's product id is 63 bits again.** The bounds added to a check-in's numbers
  held `product_id` to 32 bits, and a derived product id is 63: every camera of a project
  that had not set a small id by hand was answered 422. Caught in review before a release;
  the bound is now the column's. The product noted on a camera's registration is written
  on every check-in that stores its row, so a camera registered after it had a row is
  held to its product too, and NUL or a lone surrogate in a camera's strings is dropped
  before it reaches the store.
- **A camera's product outlives its fleet row.** The rule that a camera does not move
  itself between products applied only while it had a fleet row, and a token limited to
  one product can forget its own camera: forgotten, the camera enrolled under any product
  of the account it named. The product is now remembered on the registration and checked
  at enrolment too; only a forget by a token for the whole account clears it. A feedback
  report from a camera with no fleet row is refused (it was accepted for a release of any
  product of the account).
- **A camera's reports reach the audit trail on a daily budget, after its row is written.**
  A fallback reason that changed every check-in, eight made-up boot-journal entries each
  time, or a failed-install report as often as it liked were each an audit row and a
  webhook delivery; and a check-in the store refused left its rows behind to be written
  again. One camera now causes at most 48 such rows a day, and nothing is audited for a
  check-in whose row was not stored. `payload_version`, `publish_seq`, `product_id`, a
  stub's version and a slot's version and hash are bounded to what stores them.
- **A platform project's factory image carries the publish counter.** The counter fix
  reached `build ota-romfs` only: `build factory-romfs` took none, so a platform camera's
  factory slots were signed with 0 and seeded an anti-rollback floor of 0, and a camera
  fresh from the line accepted any release the account had ever published. The factory
  build now takes the account's next counter and refuses a platform build without one.
- **A project file frozen into firmware is a file of the project.** `[ota].ca`,
  `certs/ca.pem` and the `device/` modules were read wherever their paths led, so a
  project naming `/proc/self/environ` as its CA, or carrying a link in `device/`, put the
  build machine's secrets (a key passphrase, a token) into the firmware image. Each must
  now be a regular file inside the project, and a CA must be a PEM bundle.
- **The boot code imports nothing from a volume someone can write.** MicroPython's import
  path starts with the current directory, which the firmware sets to `/flash`, or to the SD
  card when one is in, and only then the frozen modules. `boot.py` is frozen, but its
  imports were resolved along that path: a file named `openmv_log.py`, `struct.py`,
  `hashlib.py` or `_ota_config.py` on a card ran as the firmware's own code on every boot,
  before any slot was verified. `boot.py` now sets the path to the frozen modules before
  its first import; once a slot is verified the path is that slot and then the frozen
  modules (`/rom`, `/rom/lib`, `.frozen`), so an app's own copy of a frozen library is
  still the one it imports. **An app that
  imported modules from `/flash` or the SD card must now add that path itself**
  (`sys.path.append("/sdcard")`) once it is running.
- **A check-in answer's body is under the same 30 seconds as its lines.** The body was read
  with `read(n)`, which stays in C until all `n` bytes arrive while each byte restarts the
  socket timeout, so a server that answered at once and dripped a 4 KB body held the camera
  for hours. It is read a byte per call, as the lines are.
- **A refused check-in is audited once a day per camera, not once per reason.** A refusal
  needs no key: anyone can name a registered camera and send a signature header that does
  not parse. It was audited whenever the reason differed from the last one, so two reasons
  in turn made every request a row in that account's audit trail and a delivery to each of
  its webhooks. Every refusal is still counted on the registration with its reason. The
  audit row is filed under the product the camera is in the fleet under, not the one the
  refused request named.
- **A camera that changes product by itself is audited.** A signed check-in proves which
  camera sent it; the product is whatever its body says. A camera that names a product
  other than the one it is in the fleet under, when the account did not send it there (the
  release it was last offered, or is pinned to, is not one of that product), is served and
  filed under the product it reports, and the change is a `device.product_mismatch` audit
  event, at most once a day for a camera. Pinning a camera to another product's release is how
  an account moves one, and raises no event. The product a camera is known under moves when
  the account sent it or the change was audited, so a move made after the day's event is
  still a change the next day; an offer made to a camera that moved itself no longer
  counts as the account having sent it; and a cohort's pin sends the cameras known under
  its product, not one that reported its way into it.
- **`server token revoke` and `rotate` take the hash `token list` prints.** The list shows
  the first 16 characters of each hash and revoke matched the whole hash, so given what the
  list printed it revoked nothing and said `revoked`. Both now take the start of a hash (eight characters or more),
  refuse one that names no token or several, and print the whole hash they acted on.
- **A number JSON cannot carry is a 422.** An infinite or NaN number in a request body failed
  while the refusal was being written, a 500; a rollout raised to an infinite percent by
  `PATCH` was stored and the account's rollout list stopped answering. `PATCH /rollouts`
  bounds `percent` and `failure_threshold` as creating one does (a threshold out of range is
  now a 422, not a 400), a declared `product_id` is 0..2**63-1, a manifest's `min_stub` is
  under 2**31, and a validation error names the field and not the value sent.
- **Server housekeeping.** The audit log is indexed by account (migration v44: an account's
  page of it read every account's rows). A finished webhook delivery's record is dropped
  after 30 days. Deleting an account takes its pinned roots and its list of unclaimed
  boards. The scheduled advisory scan covers the unnamed account of a self-hosted server,
  survives a database error, keeps an advisory's severity when its detail could not be read,
  and treats an SBOM whose component fields are not text as unreadable. `server check`
  redacts a credential in any URL setting, before the host or in the query. A desk key's
  revoke cannot be undone by a renewal racing it. An operation that fails part-way on SQLite
  leaves nothing for the next statement to commit. A page offset without a limit is SQL
  Postgres accepts.
- **A camera that moved itself to another product is not a rollout attempt there, and its
  data is filed under the product the account knows it by,** until the move is known. Its
  offer is not kept, so its outcome was never counted: the attempt alone thinned the
  rollout's failure rate and padded a stage's gate.
- **Rotating a token revokes the old one first, and only the caller that revoked it gets a
  replacement.** Two rotations at once left two live replacements.
- **A rollout stage's `min_soak` is a finite number.** A NaN was stored, never ramped, and
  failed the response that reported it.
- **A rollout's counters count cameras.** `attempted`, `updated` and `failures` were counted
  per transition in what a camera reported, so one camera alternating a fallback reason
  with none could fail a rollout into its auto-pause, and one alternating its version could
  pad `attempted` past a ramp stage's gate. Each camera now counts once for each, per rollout.
- **What a camera says about itself is cut to a short string.** `app_version`,
  `fallback_reason`, `slot`, `representation` and `product` are kept to 200 characters, and
  `streams` to 16 names of 64, before they reach the fleet row, the audit trail and webhooks.

- **A camera removed from the fleet could be taken into another account.** Forgetting a
  device drops its binding and keeps its registration, and the next check-in for that id,
  signed or not, was believed about its account: a tenant that kept naming the id got the
  binding, and the camera's own signed check-ins, fleet row, ingest grant and Live were
  then that tenant's. A camera registered to an account is that account's, whatever a
  check-in reports.
- **An SBOM is at most 2 MiB (was 32).** It is parsed whole at publish and at every
  advisory scan, and a 32 MiB one held every other request for a second and a half each
  time its owner asked for a scan.
- **`server check` no longer prints the database password** in `database_url`.
- **`POST /api/v1/feedback` took anyone's word.** It checked that the camera id was
  registered and nothing else: no signature, and the account read from the body. Anyone
  who knew one registered id could file failed installs and audit entries, in their own
  words, under any account, and have that account's webhooks deliver them. It takes the
  camera's signature now, files under the camera's own account, and only for a release of
  that account.
- **A product-limited token reached the account's own routes.** Webhooks (subscribe to
  every event, repoint or delete the owner's), the activity feed, roots, desk keys,
  registrations, device binding and release, cohorts by id. Those are a 403 for a limited
  token now.
- **A device pin belonged to whoever wrote it last.** Pins are keyed by device id alone, so
  another account that knew an id could hold a camera on its build with a pin to a release
  it would never be offered, or overwrite and clear the owner's pin. A pin is read only for
  the account that set it, and another account's is a 404 to move or clear.
- **A signed check-in could be replayed inside its five-minute window**, and the answer
  carries day-long Live and ingest grants for that camera. A request signed at or before
  the time of the last one taken is now stale: offers only. A feedback report is signed
  over its own context, so neither passes for the other.
- **Webhook endpoints are checked at every delivery**, not only when saved, and with
  "not a public address" in place of a list of private ranges that missed 100.64.0.0/10. A
  failed delivery records the kind of failure, not the transport's text.
- **Every route caps its body before it knows the caller.** A body was read whole before
  the credential check on any route. A camera's routes take 64 KiB, the admin API 1 MiB, and
  a release upload asks for a valid bearer token before reading a byte. `server check` no
  longer prints `keykeeper_token` and `datalake_admin_token`.
- **Rotating a product-limited token left it unlimited**, a limited token could pin its own
  device or cohort to another product's release (and download it by checking in as that
  device), and it read the account's advisories, roots, desk keys and registrations.
- **One registered camera had as many identities as spellings.** The registrar answers for
  a board name in any case or with space around it; the server kept each spelling as its
  own device, free to claim another account and be offered its release. A board name, id
  and account now have to be spelled as a camera spells them.
- **A release upload is refused by scope and by size before it is read**, and a delta part
  the manifest does not declare is refused before it is held in memory. `rotate` refuses a
  revoked token (it handed back a live one) and a token with an operator scope unless the
  caller is the root. A feedback report has to be about a release of the product the camera
  runs. The keykeeper chain route is rate-limited.
- **One tenant's token could stall the server for all of them, three ways.** A webhook
  delivery read the endpoint's whole answer into memory, inflated; it reads the status and
  nothing after. A release upload was verified and stored on the event loop, so every
  check-in waited for it; that work runs on a thread, and a manifest declares at most 32
  deltas. An advisory scan asked for while one runs waited on a worker thread; it is a 409
  now, and an SBOM that is not a component list, or lists more than 5000, is unreadable
  to the scan (it raised, or queried the advisory service once per component).
- **`server token rotate`** keeps a token's product limit and refuses a revoked token, as
  the API's rotate does.
- **A webhook delivery connects to the address it checked**, with the name as the Host
  header and TLS server name: checking a name and then handing the name to the HTTP client
  was two lookups. Each delivery is its own connection (a kept one is keyed by address,
  so it would carry the next delivery to another name there), and NAT64 addresses, which
  wrap an IPv4 address, are not public.

- **An `accounts` token could make itself the server's root.** `POST /accounts/{id}/tokens`
  let an operator issue any scope into an account it created: `accounts.all`, which sees and
  manages every account on the server, or an `accounts` token named after another operator,
  which acted as that operator (accounts are filed under the token's name). Only a token
  with `accounts.all` can issue either operator scope now; the rest get a 403.
- **An encrypted install fits on the OpenMV Cam M7 again, with room to spare.** With 15.7 KB
  of its 50 KB heap free, the largest hole was 5328 bytes, and the decompressor's first
  read needs its state (about 1.3 KB) and then its 4 KiB window: first fit put the state at
  the head of that hole and the window missed it by a hundred bytes, so any growth in the
  device library failed the install with `MemoryError`. Under 32 KB free, the installer now
  holds the window's place while the state is allocated, so the two go to different holes.
  Only a small-window image measures the heap at all: the measurement walks every block,
  and on the OpenMV N6's heap that walk outlasted a 100 ms watchdog window.
- **A check-in answer costs the camera a fixed amount of RAM and at most 30 seconds.** The
  status and header lines were read with `readline()`, which grows until a newline comes,
  so whoever answered the check-in chose how much heap it took; and every byte restarted
  the 15-second socket timeout, so an answer dripped a byte at a time held the poll loop
  for as long as it kept coming. A line is now read to its end and kept to its first 128
  bytes, and the whole answer has one deadline.
- **A download could write the slot's status sector before its hash was checked.** The
  installer streams the image over the whole slot and checks the sha256 after the last
  chunk, so bytes the stream placed in the status sector stayed on flash when the check
  failed. Whoever served the image (the update server, the image host, or the network path
  on a board whose TLS is unverified) could leave a `confirmed` marker beside an old signed
  image, which boots without the anti-rollback check, or a floor entry no image can meet,
  which no update recovers from. A published image leaves that sector blank; the installer
  refuses a stream that does not, before the write.
- **A boot stub's record no longer carries a slot past its marks or the floor.** On a
  board with a stub, `boot.py` mounted the slot the stub's install record names even when
  it was unmarked or below the anti-rollback floor, on the reasoning that the stub had
  chosen it. The stub also boots by its record when it finds no slot at all, and then the
  slot holds whatever a failed install left: on the boards whose slots are not ciphered
  (Portenta H7, Giga, Nicla Vision), an old signed image. Only an exhausted attempt budget
  still follows the record.
- **An update has to be newer than what runs.** The installer refused a manifest below the
  floor and took one equal to the running release, so a replayed current manifest with
  junk for an image erased the other slot, failed, and repeated. Refused before the erase
  (`not-newer`); recovery, where nothing runs, is unchanged.
- **Unsigned answers are held to less.** A check-in answer's `poll_after_s` is capped at a
  day and `throttled` is read from the status line alone (one answer could put the next
  check-in days out); recovery takes an offer only as an `https://` URL, as the runtime
  does; on the H7 Plus and Pure Thermal, where both slots are deciphered into one RAM
  buffer, the slot that wins is read and hashed again before it is mounted.
- **Smaller installer gates.** A manifest with no image sha256 is refused (it installed with
  no integrity check on the download). A retired key's manifest is refused by the epoch
  floor on flash as well as the running image's, which covers recovery. An update offer
  must name an `https://` URL; a path sent the installer to a file on the camera.
- **A platform project's images were all signed with a publish counter of 0.** `build
  ota-romfs` set the counter the server handed it on one signer and built the image with
  another, so the trailer and the manifest both said 0. A `PRODUCT_ID = 0` camera orders
  its images by that counter and nothing else, so it had no floor and no "not newer": it
  took any older image of the account as readily as a newer one. The counter now reaches
  the signer that signs. Projects with a product id order by version and were not affected.
- **`project setup` could run code out of a project through its lock file.** The lock
  file's MicroPython version went to `pip install mpy-cross==<version>` as written, and
  pip reads a spec with a path in it as a directory to build. Only a version goes to pip.
- **A symbolic link in the app directory is refused.** It was copied as the file it
  pointed at, so an app tree linking to a key or a token file put that file in a signed,
  published image.
- **`project keys restore` wrote wherever a backup's file names pointed.** A name in the
  archive was joined to the key directory unchecked, so `../../.bashrc` or an absolute path
  was written with the archive's bytes; its SHA-256 guards against rot, not a forgery.
  Restore takes key file names (`ota-0100.pem`, `root-0.pem`, `payload-keys.bin`) and
  refuses the archive otherwise, before writing anything.
- **Private keys at rest: 600 000 PBKDF2 rounds, and owner-only files.** The PEMs, the roots
  among them, were encrypted with the library default of 2048 rounds, and written under
  the umask. They are standard PKCS#8 still (PBES2, PBKDF2-HMAC-SHA256, AES-256-CBC), so
  existing keys open as before; `project keys add`, a new project and a new desk or issuer
  key write the new count. Key files, the backup and the client's token file are created
  mode 0600.
- **`keys backend provision` retires the keys it replaces.** The old keys left
  `trusted_keys.json` with their certificates still valid and their PEMs still on disk, and
  a key revoked but not yet retired vanished from the set, so nothing could ever make a
  camera refuse them. The new set is certified one epoch up.
- **A PKCS#11 PIN comes from `OPENMV_OTA_PKCS11_PIN`.** The only way to give one was a
  `pin` field in `keys/backends.json`, which is committed, or `--set pin=` on a command
  line. A `pin` in a record is refused.
- **A keykeeper pin vouches for the seal key.** A pin was `<key id>:<sign key>`, and the
  pinned record's own signature was never checked, so the server the chain is fetched from
  could return the pinned generation with a seal key of its own and open every per-camera
  release sealed after the next `keys keykeeper sync`. A pin carries the record's digest
  now (`<key id>:<sign key>:<sha256>`, what `openmv-ota keykeeper pin` prints and a sync
  stores). The older form still verifies a chain's first generation, by that record's
  signature, and nothing later.
- **A platform build refuses a publish counter of 2**48 or more.** The counter comes from
  the server and is signed into the release; a camera that confirms it takes nothing below
  it, so one answer near the top of the range would have ended a fleet's updates.
- **A key file is written beside its path and renamed over it**, so a link planted there is
  replaced and not written through; keykeeper's own keys are owner-only too. Restore takes
  a name by full match (a trailing newline passed) and of bounded length.
- **Smaller ones.** `git clone` of the firmware takes the remote after `--` and a commit
  that is a full hash (both come from the lock file). `client device watch --out` refuses
  a stream name from the server that is not a plain name, `release bases` a version that
  is not one, and console text is printed without its control characters.

- **The compromised eMudhra root is out of every trust store this tool ships or installs.**
  Eight eMudhra (emSign) intermediate CA keys were compromised in October 2026, and the root
  that signed them, emSign ECC Root CA - G3, left the Mozilla bundle on 2026-10-20. The host
  tool and the server now require `certifi>=2026.10.19` (what `httpx` verifies against) and
  the S3/KMS extras `boto3>=1.43.117` (botocore's own bundle); a new install or a rebuilt
  server image gets them. **A project's `certs/ca.pem` is a snapshot from the day `project
  new --ota` ran**: one created before 2026-10-20 still trusts the removed root, and so does
  the firmware built from it on the boards that freeze the full bundle (N6, AE3, RT1062).
  Download the current bundle over `certs/ca.pem` and rebuild the firmware, or ship the
  refreshed bundle as `app/lib/openmv_ota/data/ca.pem` so the next OTA release rotates the
  trust store without a reflash. Boards on `certs/root.pem` (the hosted cloud's roots) never
  trusted it.

### Changed

- **Docs match the code again.** The README gains a per-board table of what an update can
  do (deltas, recovery, the server certificate, signed check-ins, what the OTA firmware
  leaves out); the pages on signing keys, rollouts, the update server, recovery, flashing
  and webhooks say what `--new-keys`, the auto-pause floor, a superseded rollout, the
  download's rate floor, an unverified `Date`, a token's hash prefix and a delivery
  record's 30 days now do. The trailer's `wdt_en` is documented as reserved: the boot stub
  does not arm a watchdog.

- **The OpenMV Cam M4's OTA firmware drops `lens_corr()`.** Its 832 KB code region was 88
  bytes short once the boot script reset the import path. With it off the firmware has
  about 1.7 KB; `build romfs` refuses an M4 app that calls it.
- **`build romfs` refuses a dropped image method that is taken without being called.**
  `fix = img.find_barcodes` raises on the camera at the lookup, and the check only looked
  for calls. The app's own `def` of the same name is no longer mistaken for one.

- **The OpenMV Cam H7's OTA firmware drops `linpolar()`, `logpolar()` and
  `get_similarity()`.** It was 704 bytes from the top of its flash, and the installer and
  boot fixes in this release are more than that. With the three off it has about 5 KB.

- **Five boards' OTA firmware uses the small TFLM op set.** The firmware's TensorFlow Lite
  Micro library grew (openmv, 2026-11-19) and the OTA firmware stopped fitting on the
  OpenMV Cam M7, the H7 Plus, the Pure Thermal, the Arduino Portenta H7 and the Arduino
  Giga, by 8 to 61 KB. Their OTA firmware is now built with `MICROPY_PY_ML_TFLM_SMALL`: no
  comparison ops, `SelectV2` or `SquaredDifference`, so a model that uses one does not load
  there. The M7's also drops `find_datamatrices()`. The OpenMV Cam H7's stock firmware now
  ships `find_datamatrices()` off, which the build takes as agreement with its own drop.

- **The OpenMV Cam H7 Plus OTA firmware drops `find_datamatrices()`.** The stock firmware
  now fills both of that board's code regions, so the OTA firmware (frozen boot script,
  recovery installer and PEM parsing on top) no longer fits with it. `build firmware`
  turns `IMLIB_ENABLE_DATAMATRICES` off through the board-directory overlay, as it already
  does on the OpenMV Cam H7, and `build romfs` refuses an app that calls it. The stock
  firmware is unchanged.
- **Trailer v4 and signing-key certificates.** A camera now embeds only two **root** public
  keys (`keys/root_keys.json`, `ROOT_KEYS` in the firmware); every signing key reaches it as a
  160-byte certificate in the image trailer, signed by a root (`openmv_ota.ota.certificate`:
  kind, root id, epoch, P-256 key). `boot.py` verifies the certificate, then the image. The
  trailer header is a new 96-byte layout with a **component table** (blobs the boot stub
  installs to flash addresses, each with its own hash) and the stub's UART/watchdog settings;
  `pad_size`, `min_platform_version`, `key_id` and `sig_alg` are gone from it, the latter two
  now coming from the certificate. `new --ota` mints the roots and certifies every key;
  `build verify` takes `--root-keys`; `inspect` prints the certificate and components.
  A clean break: v3 images and firmware do not interoperate with v4. Certified keys are
  P-256 only.
- **OTA images carry the firmware.** On a board with a boot-stub layout in the board table
  (the N6 first: firmware at `0x70090000`, 3516 KiB), `build romfs` and `build factory-romfs`
  append the firmware `build firmware` produced -- raw LZMA1, 256 KiB dictionary -- after the
  filesystem and list it as a trailer component; `build firmware` relinks the firmware at the
  stub's destination through the board overlay. A component entry (80 bytes) carries two
  hashes: of the compressed blob (what may reach the decoder) and of the installed bytes (the
  signed digest the stub checks the decoder's output against, and what its install record
  names). `build verify` checks every component's bytes and that each decompresses to its
  installed size and hash; `partition.slots()` returns the whole body region of a slot.
- **`boot.py` follows the boot stub's install record.** On a board with a stub the firmware
  stamps `STUB_RECORD_ADDR` (the sector after the install window); `boot.py` reads the record
  the stub wrote there (`openmv_ota.ota.install_record`: stub version, slot, reason, and per
  component its destination, installed size and installed hash) and mounts the slot it names
  without burning an attempt --
  the stub did, before it installed. A record that is missing, erased, torn, or names other
  components than the slot's trailer lists is ignored and the slot choice runs as before.
- **The certificate-epoch floor.** The status sector's tail now holds two floor regions:
  the anti-rollback floor's entries (64; 29 on a stride-32 board, so both regions fit a 4 KiB
  sector), then the certificate-epoch floor's. `confirm()` raises
  the epoch floor to the running image's signing-key epoch, the installer carries it into every
  slot it writes, the factory image seeds it, and `boot.py` rejects a slot whose certificate is
  below the highest epoch across the slots (`epoch`): a retired signing key stays refused once
  a release that retired it has been kept.
- **The boot stub, and `build stub`.** `src/openmv_ota/build/device/stub/` is the C loader
  that sits at the firmware address on a stub board (the N6 first): its own Makefile and
  linker script per board, built against the HAL/CMSIS in the project's firmware checkout,
  with the LZMA decoder (LZMA SDK), P-256 (p256-m) and SHA-256 vendored beside it. It verifies
  both slots (root key -> certificate -> image signature -> component hashes), picks a slot
  the way `boot.py` would, installs the components into the firmware area when the install
  record does not already vouch for them, writes the record and jumps; the trailer's UART
  and watchdog fields pick its debug output and the watchdog it arms. `build stub` bakes the
  project's root keys, product id and attempt budget into it and emits `<board>-stub.bin`.
  Bench-verified on the N6: trial install (~4 s for a 2.2 MB firmware), confirm, fast path,
  revert + reinstall after a failed trial, and 14 resets at random points mid-install without
  a brick. The trailer's UART and watchdog fields are filled from the project's `openmv_log`
  (`ENABLED`, `UART`, `BAUD`) and `openmv_wdt` (new `STUB_WDT_ID`, `STUB_TIMEOUT_MS`).
- **Provisioning a stub board.** `build firmware` on a board with a stub layout also builds the
  stub (`<board>-stub.bin`, beside `<board>-firmware.bin`); `flash firmware` and `flash factory`
  write the two as one image -- `stub || 0xFF || firmware`, composed into
  `<board>-firmware-with-stub.bin` -- to the FIRMWARE partition, since the firmware is linked to
  run behind the stub and neither is flashable alone. The board table's stub layout gains
  `origin`, the stub's own address.
- **OpenMV Cam H7 OTA firmware drops two more methods.** The v4 `boot.py` (certificates,
  the install record, the epoch floor) and the embedded root keys grew the H7's OTA firmware
  past its 1664 KB code region by 2 KB; the board overlay now also turns off
  `find_displacement()` and `find_lbp()` (the smallest self-contained features), and
  `build romfs` refuses an app that calls them, as for `find_barcodes()`/`find_datamatrices()`.
- **The check-in names the boot stub.** `openmv_ota.status()` and the check-in body carry
  `stub: {version, reason}` from the install record `boot.py` followed (None without a stub),
  so the server can see which stubs the fleet runs before a release raises `min_stub`.
- **The update manifest carries its signing key's certificate, and the firmware keeps no key
  table.** Manifest header v2: the 160-byte certificate follows the header and the signed
  region covers header, certificate and body. `build ota-romfs` and `build factory-romfs` sign
  the manifest with the certificate the trailer carries; `build verify` chains a manifest to
  the roots like an image (`--trusted-keys` is gone, `--root-keys` serves both); `inspect`
  prints the certificate; a release's `key_id` derives from the certified key
  (`certificate.key_id_of`). The device installer verifies in `boot.py`'s order -- a root this
  firmware embeds over the certificate, the certificate's epoch against the one the camera has
  confirmed, then the certified key over the signed region -- and `_ota_config.py` carries
  `ROOT_KEYS` only: `TRUSTED_KEYS` is gone, and a revoked signing key is a raised epoch, not a
  firmware rebuild. A clean break: v1 manifests are refused.
- **The server keeps the boot stub a camera reports, and gates releases on it.** A check-in's
  `stub` lands on the device row as `stub_version` and `stub_reason` (the install reason
  decoded to `normal`, `trial`, `reverted` or `reinstalled`; `null` for a board without a
  stub, and a camera reflashed with stock firmware clears it), in `GET /devices` and
  `client devices`. The signed manifest now carries the trailer's `min_stub`, the server
  stores it per release (`min_stub` on `GET /releases`), and the offer gate -- rollouts and
  pins alike -- declines to offer a release to a camera whose stub is older than it needs,
  or that has none. The device installer refuses such a manifest before any download
  (reason `stub`), the way the stub itself would after a download and a failed trial.
- **`boot.py` follows a stub record past its own mark policy.** A record naming a slot that
  verifies (signature, certificate, board, body hash) is followed even when this firmware's
  attempt budget, floors or pending/confirmed marks would refuse the slot: the stub applied
  its policy when it installed, and mounting the other slot's app over the installed firmware
  is the one outcome the record exists to prevent.

### Added

- **HIL: a dark i.MX board revives itself over SWD.** `ci/hil/recover.py --swd-loader` (and
  the harness's pre-flash recovery, after one reset pulse) loads the CLI's bundled RAM
  flashloader through the node's J-Link -- what `sdphost jump-address` would do -- so an RT1060
  with nothing on USB (an erased firmware slot after a killed `flash factory`, or the ROM park
  after a reset) comes back as its `15a2:0073` blhost target with FlexSPI configured, and the
  normal `flash factory --in-bootloader` reprovisions it. The kick-and-catch no longer pulses a
  board whose SBL is already enumerated (a RAM loader does not survive that).
- **HIL: no REPL probe of a board that has armed its watchdog.** `_ensure_cdc` returns at once
  when the marker UART has carried `wdt: armed` since the flash: the probe's Ctrl-C stopped the
  feed, the watchdog bit, and the watchdog scenarios failed on their own forbidden
  `app.wdt_reset` marker (OPENMV4P, two runs in a row).
- **The rollout and release pages' answers, from the API.** `GET /rollouts/{id}/devices`
  (`client rollout devices`): the audience, each camera with where it stands against the release
  by payload version (`failed`, `updated`, `ahead`, `offered`, `unoffered`) and every state's
  count; `rollout status` adds `on_release` (on the release or newer) and `superseded_by`, and
  the `rollout.superseded` audit event records `by`. `GET /releases/{id}/coverage`
  (`client release coverage`): adoption, the fleet bases below the release no delta of it covers,
  and the cohorts pinned to it. Named ramps: `rollout create --ramp cautious|quick` (`ramp` in
  `POST /rollouts`), listed by `GET /ramp-presets` (`client rollout presets`). The website
  computed all of these itself.
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
- **The device page's downloads on the CLI.** `client data export` writes a text topic over a
  time window (default: the last day), oldest first across reboots, as the same CSV the console's
  download gives (or `--format ndjson`), from the datalake's new window read. `client data series
  --format csv` writes a chart as rows, several fields side by side. A cell that opens like a
  formula is written as text: a camera's console line must never run in a spreadsheet.
- **`client data usage`: the datalake's meter for your account.** Bytes stored, today's uploads
  against the daily quota, the uploads it refused today, and the retention window, from the new
  `GET /api/v1/admin/data/usage` (`observe`). Past the quota the datalake refuses everything until
  midnight UTC, and until now nothing a customer could read said so.
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
  camera with no registration, gets nothing. Recovery signs once its clock is set.
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

- **`flash erase` on the Portenta H7 and the Giga no longer erases firmware code.** Its first
  step wrote zeros at `0x08020000`: the internal filesystem of the stock firmware, and part of
  the firmware (its second text region) on an OTA build, which keeps the filesystem on the
  QSPI part. The camera did not boot until it was flashed again. The erase is the QSPI
  filesystem alone now, as on the Nicla Vision.
- **A slot conversion copies the whole of a slot's control data.** The boot stub copied the
  last erase block of a clear slot as its status sector and trailer. That is both on a 64 KiB
  block; on 4 KiB blocks it was the trailer alone, and the converted slot was refused for its
  blank status sector. The control data is the last 8 KiB, whatever the block size.

- **A camera passes over an update artifact under a cipher it does not have.** The installer
  chose the smallest copy of an update it could use by format alone, so a release that one
  day carried a copy under a newer cipher would have been picked by every camera in the
  field and failed. Copies whose `enc.alg` the installer does not know are skipped, as an
  unknown format always was -- in the choice, and in the full image a failed delta falls
  back to.

- **A boot journal entry with more numbers than the server knows is kept.** The server took
  an entry from a check-in only if it was exactly six numbers long, so a stub that later
  added a seventh would have had every entry dropped without a word. The first six are the
  entry; the rest is ignored.

- **An Arduino H7 camera no longer boots without its slots after a reset.** On the Portenta
  and the Giga about one reset in twenty of the running firmware left the boot stub unable to
  reach the QSPI flash that holds the slots (`qspi reset timeout`): it then booted from the
  install record alone, with nothing to fall back on. The flash controller had no clock. The
  firmware runs the controller from PLL2; a reset turns PLL2 off and puts the controller's
  clock selector back on the bus clock in the same instant, and the selector finishes that
  switch only on edges of the clock it is leaving. The stub now runs PLL2 for a millisecond
  before it first uses the controller, and puts PLL2's registers back. Portenta bench, resets
  of the running firmware: 4 of 65 without the change, 0 of 120 with it. The fleet now fails
  any scenario in which a stub says the flash never answered.
- **The STM32 boot stub's debug UART no longer garbles a line around a delay.** The stub slept
  with a character still in the transmitter; the STM32N6 gates a peripheral's clock while the
  core sleeps, so the end of one log line and the start of the next came out as junk each
  time the flash controller was set up. The stub now lets the UART finish before it sleeps.
- **The STM32H7 stub resets the QSPI part on 1 line before 4.** The fleet's Portenta
  booted without its slots ("qspi reset timeout") on about one debug-core reset in ten:
  the controller stuck on the stub's very first instruction, the reset-enable sent on 4
  lines, with the state the bootloader hands over identical to a clean boot's. The
  1-line pair now goes first, the 4-line pair (for a part left in QPI) after it: 0 of 60
  such resets on the bench against 2 of 20 before. The stub also logs the inherited
  controller registers on every boot and the fault registers behind the jump's stray fault.
- **The STM32H7 stub invalidates the data cache itself before enabling it.** The OpenMV Cam
  H7 Plus logged a stray bus fault at the jump on one boot in three -- after a debug-core
  reset of the running firmware, or on the boot that installs: a buffered write to an
  address nothing in the stub writes, refused by the slave (the M7's ABFSR is now in the
  log). An explicit invalidate before CMSIS's enable, which also invalidates and was not
  enough, ends it: 0 faults in 23 bench boots against 6 of 8 install boots before.
- `build firmware --incremental` works across invocations. The wrapper directory (frozen
  manifest, board overlay, mbedtls config) was a fresh temp dir each build, so the objects'
  dependency files named headers that no longer existed and the second build stopped on
  "No rule to make target". It is now `build/openmv-ota-<board>/` in the firmware tree,
  rewritten every build and left in place; `--keep-build-dir` reports its path.
- `build firmware` on the STM32H7 boards no longer fails the relink when the main window
  is short by less than the second text region's alignment margin: it repacks the region
  with the margin shrunk by the shortfall and links again (up to four attempts, the
  linker deciding), and names the regions and bytes when the firmware truly does not fit.
- **A camera with an armed watchdog reset right after an install's TLS handshake.** The
  OPENMV4P `watchdog` leg, three runs in a row: `install: TLS up`, then a boot from the watchdog
  before `install: fetched body`. The handshake fills the heap with mbedtls temporaries, and the
  reader's buffer and the header lines are the next allocations; the first that did not fit ran
  an automatic collect, 65-100 ms on this core, inside the 100 ms window with nothing feeding.
  The installer now collects on purpose right after the handshake, fed on both sides, as the
  slot survey already did (`_collect_fed`). No-op without a watchdog.
- **HIL: the pre-flash recovery stops probing a board that re-arms its watchdog.** The first
  leg of a job finds the previous job's watchdog app still running; a REPL probe stops its feed,
  the board bites and reboots into the same app, and the next probe did it again -- three
  probes, three reset pulses and a minute and a half before the romfs erase that was coming
  anyway. Once the board says `wdt: armed` after a probe, the recovery goes straight to the
  erase before a golden flash, and leaves the board alone after one.
- **The N6 boot stub's debug UART printed one byte and went silent.** The bootloader leaves
  PLL3 off, and with it off the STM32N6's USART kernel-clock mux never delivers a clock to the
  USART, whichever source is selected: the transmitter never acknowledges enable and the first
  byte sits in the data register until the firmware's clock setup turns PLL3 on. The stub now
  brings the clock tree up the way the firmware does, from the firmware's own board macros
  (`board_config.h`: the oscillators and PLL3/PLL4 the board names, IC14 from PLL1) and runs
  every UART from IC14 as `uart_init0` does, with the baud taken from the HAL's view of that
  clock. The stub's Makefile adds the firmware's board directory to its include path for it.
- **`flash` reaches an Arduino board whose app arms a watchdog.** The 1200-baud touch makes
  the firmware jump to the bootloader 250 ms later; an app with a 100 ms watchdog armed resets
  first, and the app re-arms about 6 s after it boots. `flash` used to wait 20 s for DFU and 60 s
  more for the port before touching again, so the second touch always met a re-armed app and
  the command failed with "did not enter its DFU bootloader after 2 1200-baud touches". It now
  watches the port and touches again the moment the app re-enumerates, before it can re-arm,
  for up to 90 s (measured on the Portenta H7, Giga and Nicla Vision: DFU within 2-3 s, two
  touches).
- **A reset in the middle of a secure-element exchange no longer strands a camera's keys.** The
  interrupted SE050 refused every request until its power was cut, so the camera could not sign
  a check-in until someone power-cycled it. The OpenMV Cam RT1062 now cuts the chip's power
  itself: boot finds the chip not answering, deep sleeps for 2 seconds and boots again, once,
  before an update's trial boot is counted (it needs firmware with MicroPython #19619's
  `machine.DEEPSLEEP_RESET`). The Arduino Giga's ATECC608 waits out the chip's own 1.3 s
  watchdog when it does not wake. The Portenta H7 and Nicla Vision already power-cycle their
  SE050 through its enable pin.

- **A refused rollout no longer stops the live one.** Creating a rollout paused the cohort's
  active rollout as superseded before checking the new one's stages, so a malformed ramp left the
  cohort with nothing offering and answered 400. Every refusal now comes first.
- **A new project's app no longer spends its account's data quota by mid-afternoon.** The
  generated `main.py` logged a heartbeat line every second, and every log line uploads: about
  5 MB a camera a day. Eight cameras spent the default 50 MB/day datalake quota by 14:27 UTC,
  after which the datalake refused everything until midnight, heap graphs included. The heartbeat
  now logs once a minute.
- **A camera in firmware recovery shows on the fleet view again.** Recovery's check-in went
  unsigned, and with signed check-ins required that meant update offers and no fleet row. It now
  signs with the camera's identity key once its clock is set (NTP first, else the reply's `Date`),
  so a recovering camera reads as recovering; the first, unsigned, check-in still gets the offer.
  The OpenMV Cam M4, which has no network, no longer carries recovery in its firmware at all: it
  could never run there, and signing pushed the M4's firmware over its flash.
- **A camera whose network stack runs out of memory heals itself.** An H7 Plus whose WINC ran out
  of buffers after a relay send stalled failed every check-in with `OSError(12)` -- at 7% heap
  use -- and never recovered: the transport rebuild waited for five failed cycles, half an hour
  at that pace, and nothing came after it. A check-in still out of memory after the optional
  connections are closed now rebuilds the transport at once and, if the next one is too, reboots
  (only a camera that has checked in since boot, so an app that starves itself can't loop). The
  live relay waits a minute after an out-of-memory failure instead of five seconds.
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

### Fixed

- **`build firmware --incremental` cleans when the tool's own inputs changed.** The board
  overlay, the frozen manifest and the injected modules live in a per-build temporary
  directory `make` never sees, so an incremental build after a board stopped dropping a
  decoder (or gained a stub relink) kept objects compiled against the old `imlib_config.h`.
  Each build now records a digest of those inputs under `build/<TARGET>/`; a differing digest
  forces the clean, a matching one (or none, from a tree built before the stamp) keeps the
  flag's word. `-j` is not an input.
- **A relaxed watchdog region no longer allocates on entry.** `openmv_wdt` makes its one
  hard soft-timer when the watchdog is armed (`start()`, or the first `stall_guard()`) and keeps
  it for the life of the app; `relax()` and `stall_guard()` only adjust the ISR's budget on the
  way in and out. The old design made a `machine.Timer` on every `relax()` entry and tore it
  down on exit, which put an allocation -- and the automatic garbage collection it can trigger,
  65-100 ms on an H7 -- in the one gap nothing was feeding: between the TLS handshake's region
  and the collect that follows it. That is where the OpenMV Cam H7 Plus bit its 100 ms WWDG
  right after `install: TLS up` on the `watchdog` leg. The installer also logs
  `install: collected`, `install: request sent` and `install: status line` at DEBUG, so the next
  such bite says which step it landed in.
- **The STM32H7 stub reports the QUADSPI state it inherits.** Three lines per boot
  (`qspi inherited cr|sr|ccr`, before its first peripheral reset) plus the address, length and
  device registers, the flash option bytes and the AHB3 reset register in the failure dump, for
  the Portenta/Giga controller wedge at a debug-core reset. The stray-fault line at the jump
  (every H7 Plus boot, step 5) now names the fault status and address registers too.

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

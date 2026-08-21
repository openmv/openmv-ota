# Building

*[← 5 · Signing keys](05-signing-keys.md) · [Index](00-introduction.md) · [7 · Factory & firmware →](07-factory-and-firmware.md)*

---

This page is `build romfs` — how a project's app becomes the image a camera runs —
and the compilation machinery the rest of `openmv-ota build` reuses.

## build romfs

`build romfs` reads a project, compiles the app the way the pegged firmware
expects, and packs a ROMFS image for each target board:

```bash
openmv-ota build romfs ./my-product
```

For a **non-OTA** project the output is the ROMFS body,
`<project>/build/<board>-romfs.img`. An **OTA** project writes a signed bundle,
`<board>-romfs.zip`, instead. A multi-core board also builds its coprocessor
partition as a plain `<board>-coprocessor-romfs.img`, and an OTA build nests that
image inside the main one for
[`openmv_ota.sync()`](04-ota-projects.md#multi-core-boards-a-coprocessor-partition)
to apply on-device.

Every image carries the generated, read-only
[`/rom/system.json`](02-projects.md#systemjson-generated-read-only) — identity and
provenance, composed from the config and the lock. Capacity is the whole partition
for a plain project and the [OTA slot budget](04-ota-projects.md#what---ota-changes)
otherwise; the build summary reports usage against whichever bound applies.

The app source defaults to `<project>/app`; pass `--app` for another directory.

The pegged firmware checkout must match the lock and be clean. The app is
compiled with that checkout's own tools — the `.mpy` bytecode must match the
firmware it will run on — and the image stamps the lock's provenance, so a
drifted tree would build an image whose claims don't describe its bytes.
`build romfs` refuses it; `project status` shows the difference and
`project sync` re-pegs. This is distinct from `openmv-ota romfs pack`, which
packs a directory verbatim with no compilation.

### OTA signing

For an OTA project, `build romfs` stamps and signs a **trailer** onto each image,
turning a bare ROMFS body into a verifiable, anti-rollback OTA image. No extra
flags — the signing context comes from the project:

- **App version → payload version.** `app_version` from `app/settings.json` is
  encoded into the trailer's `payload_version` as
  `(major<<24)|(minor<<16)|(patch<<8)`, the monotonic anti-rollback counter. Bump
  it there for each release.
- **Signed with the current OTA key.** The signer is `[ota].signing_key_id` from
  `openmv-ota.toml`; the trailer records `key_id` + the COSE algorithm so the
  device selects the matching trusted public key.
- **Identity + provenance stamped in.** `product_id` / `board_name` come from the
  `[targets.<BOARD>]` tables, versions and commits from the lock — exactly the
  `system.json` fields, and the trailer's metadata carries a **verbatim copy of
  `system.json`**, so host tools read an image's identity without mounting the
  ROMFS. `min_platform_version` is the pegged firmware's version code.

**The key's passphrase.** The private keys are encrypted at rest, so a signing
build resolves their passphrase in priority order: the project's cached dev
passphrase when present, then `--key-passphrase-file`, then the
`OPENMV_OTA_KEY_PASSPHRASE` environment variable (what CI uses), and finally an
**interactive prompt** on a terminal — day to day you simply type it; the file
flag exists for scripts. (Passphrases travel in files or the environment, never
on the command line where they would land in shell history and `ps` — and signing
accepts the environment where provisioning refuses it, deliberately: a wrong
value here fails loudly and signs nothing, while a wrong value at provisioning
would silently seal the key supply.) A **dev-keyed** project signs without any of
this, but the build refuses to produce a production image unless you pass
`--allow-dev-key`.

The bundle keeps its two pieces as separate zip *entries*:

| Entry | What |
|---|---|
| `romfs.img` | the ROMFS body (mounted at `/rom`, written to the slot start) |
| `trailer.bin` | the signed trailer (written to the slot's last erase block) |

One file is easier to flash, upload, and track — and because a zip is
random-access, the update server reads `trailer.bin` (version / `product_id` /
signature / the `system.json` copy) without touching the multi-MB body. The device
never gets the zip (it can't hold the body in RAM to unzip): a server streams body
and trailer separately, exactly as they're placed on-flash. Every trailer field is
final and signed, including `pad_size` and the crc32.

`build romfs` fails (exit 1) on an incomplete signing context — a missing or
unreadable `app/settings.json`, a missing or non-semver `app_version`, a
`signing_key_id` not in `keys/trusted_keys.json`, or a missing private key (only
the signing machine has `keys/private/`). It *warns* but builds if a target's
`product_id` is `0` (you overrode the auto-assigned id, turning the cross-flash
guard off) or if two boards collide on one id (the guard can't tell them apart).

### Compiling

`.py` files are compiled to `.mpy` with the project's mpy-cross; models
(`.tflite`, `.lite`, `.onnx`) are converted for the board's NPU with the
project's Vela (AE3) or ST Edge AI (N6). A model that is already converted is
packed unchanged.

mpy-cross is the binary the firmware build produced when present, else a
pip-installed `mpy_cross` (as the IDE uses — no C compiler needed).
`project setup` installs the matching version; to install it yourself, match the
firmware's MicroPython version (`project show` reports it):

```bash
pip install mpy-cross==1.28.0
```

If neither is available, `build romfs` prints that command.

### Tool arguments

The compilers run with the board's pegged arguments; add your own with the
per-tool flags (repeatable — use the `=` form for a value that begins with `-`):

```bash
openmv-ota build romfs ./my-product --vela-arg=--verbose-all --mpy-arg=-O2
```

| Flag | Effect |
|---|---|
| `-b, --board NAME` | Build only this board (repeatable; default: all targets). |
| `--app DIR` | Main app source directory (default: `<project>/app`). A coprocessor partition always builds from `<project>/app-coprocessor`. |
| `-o, --output DIR` | Output directory (default: `<project>/build`). |
| `--no-compile-py` | Pack `.py` as source instead of compiling. |
| `--no-convert-models` | Pack models as-is instead of converting. |
| `--mpy-arg ARG` | Extra mpy-cross argument (repeatable). |
| `--vela-arg ARG` | Extra Vela argument (repeatable). |
| `--stedgeai-arg ARG` | Extra ST Edge AI argument (repeatable). |
| `--vela-optimise {Performance,Size}` | Vela optimisation mode (default: Performance). |
| `--stedgeai-optimization {0,1,2,3}` | ST Edge AI level (default: 3 = max). |
| `-f, --firmware PATH` | Firmware checkout override. |
| `--allow-oversize` | Warn instead of failing when an image exceeds the budget. |
| `--keep-build-dir` | Keep the staging directory for inspection. |

---

*[← 5 · Signing keys](05-signing-keys.md) · [Index](00-introduction.md) · [7 · Factory & firmware →](07-factory-and-firmware.md)*

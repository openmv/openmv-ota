# Release artifacts

*[← 7 · Factory & firmware](07-factory-and-firmware.md) · [Index](00-introduction.md) · [9 · Flashing →](09-flashing.md)*

---

The previous page built what a device ships with — the factory image and the
firmware that boots it. This page is what a camera downloads after that: the
publishable release set, plus the SBOM that documents a build's dependencies
and the read-only commands that check any built image.

## build ota-romfs

`build ota-romfs` produces the complete publishable set for a release, straight
from app source — it runs the same compile-and-sign as
[`build romfs`](06-building.md#build-romfs) internally (same flags), then renders
what a camera actually downloads:

```bash
openmv-ota build ota-romfs ./my-product
# -> build/<board>-ota.img.gz       the gzipped slot-sized image
#    build/<board>-manifest.bin     the signed manifest install() fetches first
```

The **manifest** is the descriptor the device fetches before anything else: it
names the image's size and sha256 and the available representations, and binds
`product_id` / version / anti-rollback under the same ECDSA key as the image. The
trailer is its source of truth, so there is no separate metadata file to keep in
sync. Representation URLs inside it are **relative filenames**, resolved on-device
against the manifest's own location — host the artifacts beside each other and the
signed manifest moves between hosts without re-signing.

### Deltas (`--delta-from`)

```bash
openmv-ota build ota-romfs ./my-product --delta-from build/bases
# -> additionally: build/<board>-ota.delta-<base-version>.gz
```

`--delta-from` takes a base to diff against — a factory image, a previous
release's `-ota.img.gz`, or a directory of either — and emits a compressed patch
a camera applies against the release it is already running, downloading only the
changes. It is repeatable because a fleet mid-rollout is spread over several
versions: ship one delta per base version still in the field, and a device with
no matching base takes the full image. The delta is pure transport — the
reconstructed slot is still sha256- and signature-verified on the device.

`--allow-republish` permits re-signing a version at or below the last published
one — a dev-loop convenience the server mirrors with a flag of the same name.

## build sbom

```
openmv-ota build sbom .            # -> build/sbom.cdx.json
openmv-ota build sbom . -o -       # print to stdout
```

Exports the project's dependency **SBOM** as CycloneDX 1.5 JSON, rendered
entirely from the committed lock — the firmware commit, every submodule commit
and remote, the MicroPython version, and the resolved toolchain versions are
already the lock's job, so this is a renderer, not new data collection. It needs
only the committed project (config + lock + `app/settings.json`): no firmware
checkout, so CI can export it from a bare clone.

```
$ openmv-ota build sbom ./orchard-sentry
wrote orchard-sentry/build/sbom.cdx.json (10 components)
```

The root component is your product at its `app_version`; the openmv firmware and
every github-hosted submodule carry `pkg:github` purls pinned to exact commits,
and the toolchain (SDK, mpy-cross, vela, ST Edge AI) appears at its resolved
version. Output is **deterministic**: the BOM's timestamp is the lock's
`generated_at` and there is no serial number, so the same lock renders
byte-identical JSON — an SBOM that changes only when a dependency changes is
diffable evidence.

| Flag | Effect |
|---|---|
| `-o, --output FILE` | Where to write (default `<project>/build/sbom.cdx.json`; `-` prints to stdout). |

## Inspecting and verifying

Two read-only commands operate on any built image: the `<board>-romfs.zip` bundle,
the loose `romfs.img` / `trailer.bin`, or a `<board>-factory-romfs.img` — the
factory image is a dual-slot partition, so both commands locate each slot's
trailer (scanning block-aligned offsets, CRC-validating) and report or verify
**each slot** independently. A plain, unsigned romfs (a non-OTA `-romfs.img` or a
`-coprocessor-romfs.img`) has no trailer: `inspect` says so and exits 0, while
`verify` says there is nothing to verify and exits non-zero — never mistaken for
"verified".

### build inspect

```bash
openmv-ota build inspect build/OPENMV_N6-romfs.zip
openmv-ota build inspect build/OPENMV_N6-factory-romfs.img   # prints slots A + B
```

Decodes the signed trailer and prints it: product / board / `product_id` /
`board_name`, the app version (and the `payload_version` / `min_platform_version`
it encodes, shown as semver), the signing key and algorithm, the body size +
SHA-256, and a provenance line. `--json` dumps the full structure for scripting.
It does no crypto — it just reads the trailer.

### build verify

```bash
openmv-ota build verify build/OPENMV_N6-romfs.zip
```

The host-side **authenticity + integrity** gate — the mirror of what the device's
`boot.py` checks, for CI or pre-publish. It confirms the trailer parses, the
signing `key_id` is trusted **and not revoked**, the algorithm matches, the
**signature verifies**, and the body matches the signed size + SHA-256. Exit 0 on
success, 1 on a verification failure (with the reason), 2 on a bad argument. For
a factory image every slot is verified and any failing slot fails the command
(verdicts printed with `A:` / `B:` prefixes). Trusted keys come from
`--trusted-keys` (default `keys/trusted_keys.json`), so running from a project
root just works.

It deliberately does **not** check the device-relative fields — `product_id`
against a device, `payload_version` against the installed image,
`min_platform_version` against the running firmware — those need a device, and
remain `boot.py`'s job.

## See also

- [Trailer format](../reference/trailer.md) — the on-flash layout of the signed trailer.

---

*[← 7 · Factory & firmware](07-factory-and-firmware.md) · [Index](00-introduction.md) · [9 · Flashing →](09-flashing.md)*

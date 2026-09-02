# The openmv-ota tutorial

*[Index](00-introduction.md) · [1 · The ROMFS →](01-romfs.md)*

---

**openmv-ota** is OpenMV's tooling for updating cameras in the field. You build your
MicroPython application into a signed image on your computer; a camera downloads that
image over the network and installs it; and if the new image misbehaves, the camera
falls back to the version that last worked.

Everything on these pages is driven by one command-line program — "the CLI" from
here on:

```bash
pip install openmv-ota
```

Its commands are grouped by **verb**:
`openmv-ota project …`, `openmv-ota build …`, and so on, and each page of this
tutorial covers one verb. Two pieces run somewhere other than your computer:

- the **device runtime** — `boot.py` and a small library that run on the camera
  itself ([pages 10–11](11-boot-and-rollback.md)), and
- the **update server** — a web service that hosts what you publish and decides
  which camera is offered what ([pages 15–23](15-the-client.md)). You drive it
  with the `client` verb; other software (such as OpenMV's cloud) drives the same
  **HTTP API** the server exposes.

## The words every page uses

- **ROMFS** — the read-only filesystem a camera mounts at `/rom`. Your application
  ships as a ROMFS **image**: one file containing that whole filesystem.
  ([Page 1](01-romfs.md) explains the format and the tool that makes them.)
- **firmware checkout** — a local git clone of the OpenMV firmware. A **project**
  ([page 2](02-projects.md)) is *pegged* to one: it records exactly which firmware
  commit and which tool versions your images are built against, so a build is
  reproducible anywhere.
- **signing** — an update image carries a cryptographic signature in a footer
  called the **trailer**. A camera refuses an image that is unsigned, tampered
  with, or older than what it already ran (**anti-rollback**).
- **slots** — on an update-capable camera the ROMFS partition holds **two images
  side by side, A and B**. An update writes into the slot that is not running; at
  boot the newest valid image wins, so a bad update falls back to the other slot.
  A freshly installed image boots as a **trial**: your application must **confirm**
  it works, or the next boot falls back. Boards too small for two slots run in
  **single-image mode** instead.
- **release / rollout / fleet** — a published image is a **release**; the update
  server offers it to a chosen share of your **fleet** of cameras — a **rollout**
  — which you widen, pause, or roll back as confidence grows. Rollouts target a
  **cohort**, a named group of your devices. Cameras periodically **check in**
  with the server: each check-in reports what the camera is running and is the
  moment it can be offered an update.

Read the pages front to back the first time; after that each page stands alone.
Every command and flag on these pages exists — the test suite holds the CLI to it.

| Page | Covers |
|---|---|
| [1 · The ROMFS](01-romfs.md) | what `/rom` is, and `openmv-ota romfs` — pack, unpack, inspect, verify |
| [2 · Projects](02-projects.md) | `openmv-ota project new` — pegging to a firmware, and the app folder |
| [3 · The lock](03-the-lock.md) | `setup` / `show` / `status` / `verify` / `sync` / `history` — keeping the peg honest |
| [4 · OTA projects](04-ota-projects.md) | what `--ota` changes: slots, the scaffolded device library, board identity |
| [5 · Signing keys](05-signing-keys.md) | `project keys` — the provisioned key set, rotation, revocation |
| [6 · Building](06-building.md) | `build romfs` — the build engine: compiling the app into the ROMFS image |
| [7 · Factory & firmware](07-factory-and-firmware.md) | `build factory-romfs` + `build firmware` — what a device gets at manufacture |
| [8 · Release artifacts](08-release-artifacts.md) | `build ota-romfs` / `sbom` / `inspect` / `verify` — the publishable set, its signing, and its checks |
| [9 · Flashing](09-flashing.md) | `openmv-ota flash` — every board's programming path |
| [10 · Erase & bootloader](10-erase-and-bootloader.md) | the maintenance verbs — wiping the user disk, writing the bootloader itself |
| [11 · Boot and rollback](11-boot-and-rollback.md) | `boot.py` — slot selection, the trial, falling back |
| [12 · The device library](12-device-library.md) | `openmv_ota` on the camera — `status` / `confirm` / `sync` / `install` |
| [13 · Logging & the watchdog](13-logging-and-watchdog.md) | `openmv_log` / `openmv_wdt` — the frozen survival modules |
| [14 · Recovery](14-recovery.md) | firmware-resident recovery — what runs when no slot is bootable |
| [15 · The client](15-the-client.md) | `openmv-ota client` — logging in and publishing |
| [16 · Cohorts and rollouts](16-cohorts-and-rollouts.md) | grouping devices, staging a release, pinning exceptions |
| [17 · Watching the fleet](17-watching-the-fleet.md) | the four reads + scripting with `--json` |
| [18 · Building deltas](18-building-deltas.md) | the next release, built against what the field runs |
| [19 · Accounts and tokens](19-accounts-and-tokens.md) | the tenancy layer — credentials, scopes, device binding |
| [20 · The update server](20-update-server.md) | what the service does — its guarantees, and one check-in step by step |
| [21 · Self-hosting](21-self-hosting.md) | `openmv-ota server` — running your own: lifecycle, settings, deploy artifacts |
| [22 · The device API](22-device-api.md) | what a camera speaks: check-in, downloads, feedback |
| [23 · The admin API](23-admin-api.md) | the auth model and API conventions — the endpoint reference lives at your server's `/docs` |

Not part of the walkthrough, but referenced from it:

- [`docs/reference/`](../reference/) — the engineering notes: the
  [signed trailer
  format](../reference/trailer.md), the [ROMFS image
  anatomy](../reference/romfs-format.md), the [threat model](../reference/threat-model.md),
  and [CI](../reference/ci.md).
- [`docs/compliance/`](../compliance/) — the [CRA / RED
  mapping](../compliance/cra-red-alignment.md) and the shipped fill-in templates.

---

*[Index](00-introduction.md) · [1 · The ROMFS →](01-romfs.md)*

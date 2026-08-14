# The openmv-ota tutorial

*[Index](00-introduction.md) · [1 · The ROMFS →](01-romfs.md)*

---

**openmv-ota** is OpenMV's tooling for updating cameras in the field. You build your
MicroPython application into a signed image on your computer; a camera downloads that
image over the network and installs it; and if the new image misbehaves, the camera
falls back to the version that last worked.

Everything on these pages is driven by one command-line program, `openmv-ota`,
installed with pip — "the CLI" from here on. Its commands are grouped by **verb**:
`openmv-ota project …`, `openmv-ota build …`, and so on, and each page of this
tutorial covers one verb. Two pieces run somewhere other than your computer:

- the **device runtime** — a small library that runs on the camera itself
  ([page 5](05-device-runtime.md)), and
- the **update server** — a web service that hosts what you publish and decides
  which camera is offered what ([page 6](06-update-server.md)). You drive it with
  the `client` verb; other software (such as OpenMV's cloud) drives the same
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
| [1 · The ROMFS](01-romfs.md) | installing the tools; what `/rom` is, the image format on flash, and `openmv-ota romfs` |
| [2 · Projects](02-projects.md) | `openmv-ota project` — pegging to a firmware, keys, the lock |
| [3 · Building](03-building.md) | `openmv-ota build` — romfs / factory-romfs / firmware / ota-romfs |
| [4 · Flashing](04-flashing.md) | `openmv-ota flash` — every board's programming path |
| [5 · The device runtime](05-device-runtime.md) | what runs on the camera — `status` / `confirm` / `sync` / `install` |
| [6 · The update server](06-update-server.md) | `openmv-ota server` + `client` — hosting releases, rollouts, the admin API |

Not part of the walkthrough, but referenced from it:

- [`docs/reference/`](../reference/) — the design and engineering notes: the
  [architecture](../reference/architecture.md), the [signed trailer
  format](../reference/trailer.md), the [threat model](../reference/threat-model.md),
  [CI](../reference/ci.md), the [v2 design](../reference/v2-plan.md) and its
  [hardware results](../reference/v2-hardware-results.md).
- [`docs/compliance/`](../compliance/) — the [CRA / RED
  mapping](../compliance/cra-red-alignment.md) and the shipped fill-in templates.

---

*[Index](00-introduction.md) · [1 · The ROMFS →](01-romfs.md)*

# The openmv-ota tutorial

*[Index](00-introduction.md) · [1 · Getting started →](01-getting-started.md)*

---

This tutorial is the reference for the CLI and the API, in the order you actually use
them: install the tools, peg a project to a firmware, build signed images, flash a
board, wire the device runtime into your app, and run the update server that stages
releases across a fleet. Each page is complete for its verb — every command and flag on
these pages exists, and the test suite holds the CLI to that.

Read it front to back the first time; after that each page stands alone.

| Page | Covers |
|---|---|
| [1 · Getting started](01-getting-started.md) | installing the tools and the SDK |
| [2 · Projects](02-projects.md) | `openmv-ota project` — pegging to a firmware, keys, the lock |
| [3 · Building](03-building.md) | `openmv-ota build` — romfs / factory-romfs / firmware / ota-romfs |
| [4 · Flashing](04-flashing.md) | `openmv-ota flash` — every board's programming path |
| [5 · The device runtime](05-device-runtime.md) | what runs on the camera — `status` / `confirm` / `sync` / `install` |
| [6 · The update server](06-update-server.md) | `openmv-ota server` + `client` — hosting releases, rollouts, the admin API |
| [7 · The romfs tool](07-romfs.md) | `openmv-ota romfs` — the low-level image packer underneath it all |

Not part of the walkthrough, but referenced from it:

- [`docs/reference/`](../reference/) — the design and engineering notes: the
  [architecture](../reference/architecture.md), the [signed trailer
  format](../reference/trailer.md), the [threat model](../reference/threat-model.md),
  [CI](../reference/ci.md), the [v2 design](../reference/v2-plan.md) and its
  [hardware results](../reference/v2-hardware-results.md).
- [`docs/compliance/`](../compliance/) — the [CRA / RED
  mapping](../compliance/cra-red-alignment.md) and the shipped fill-in templates.

---

*[Index](00-introduction.md) · [1 · Getting started →](01-getting-started.md)*

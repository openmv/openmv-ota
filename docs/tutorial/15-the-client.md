# The client

*[← 14 · Recovery](14-recovery.md) · [Index](00-introduction.md) · [16 · Cohorts and rollouts →](16-cohorts-and-rollouts.md)*

---

`openmv-ota build ota-romfs` leaves a signed release in `build/`; nothing so far decides
which camera downloads it. That is the **update server**'s job — a central service that
hosts your releases and stages them across the fleet — and **`openmv-ota client`** is how
you drive it. Everything the client does goes through the server's admin HTTP API,
so anything it can do, your own scripts and dashboards can do too.

This page is the entry point: logging in and publishing. The pages after it stage
releases to cohorts, operate and watch the live fleet, and manage accounts. You need
two things — a **server URL** and an **admin token**.
On the OpenMV-hosted service (the default) both come with your account; a self-hosted
server issues its own.

## Logging in

`client login` saves your credentials so no later command needs them. The server URL
defaults to the OpenMV-hosted service, so out of the box only the token is needed:

```
$ openmv-ota client login --token <admin-token>
saved /home/you/.config/openmv-ota/client.toml

$ openmv-ota client logout
removed /home/you/.config/openmv-ota/client.toml
```

The token can also arrive on stdin or from `OPENMV_OTA_TOKEN`, so it never has to appear
in shell history. Every verb resolves its credentials the same way:

| source | when it wins |
|---|---|
| `--server` / `--token` on the verb | always (a one-off against another server) |
| `OPENMV_OTA_SERVER` / `OPENMV_OTA_TOKEN` | when no flag is given — how CI runs stateless |
| `~/.config/openmv-ota/client.toml` | what `login` wrote (mode 0600) |
| `https://ota.cloud.openmv.io` | the built-in fallback for the server URL — the OpenMV-hosted service |

Only the URL has a built-in fallback; the token is always yours to provide.

## Publishing a release

`client release publish` uploads the exact signed bytes the build produced — the manifest, the
full image, and every delta the manifest declares:

```
$ openmv-ota client release publish ./my-product -b OPENMV_N6
published rel_4f9c2a81d06b73ee  version 1.2.0  (full, ocdl)
```

The parenthetical lists the release's **representations** — the forms a device can
download it in: `full` is the whole image, `ocdl` is a delta patch (`ocdl` is the patch
format's name). Step by step:

1. It picks up `<board>-manifest.bin` and `<board>-ota.img.gz` from `build/` (or
   `-o DIR`). Every release has that one **full image**; it may also carry **deltas** —
   small patches, each against one specific older release (its *base*), so a device
   running that base downloads only the changes. The **signed manifest** names exactly
   which deltas belong to this release, and that is what publish reads — a declared delta
   missing from the directory is an error here, where the fix is local (`build ota-romfs`
   again), not a rejection from the server.
2. It also attaches the release's **SBOM** — the standard machine-readable list
   (CycloneDX) of every dependency and version built into this firmware, generated from
   the project's lock file. The server stores it beside the release, so "what exactly is
   in the release the fleet is running?" stays answerable later (CVE scans, compliance).
   If the SBOM can't be generated, publish proceeds anyway with a warning — it must never
   be the reason a release doesn't ship.
3. The server derives **all** release metadata (product, version, sizes, hashes, your
   account) from the signed manifest — never from anything the client asserts — and
   refuses an upload whose artifacts don't match it: an image whose sha256 or size
   disagrees, a declared delta that didn't arrive, an extra delta the manifest never
   named, a delta whose target size is wrong.
4. Publish-time anti-rollback: a `payload_version` at or below the newest already
   published for that product is refused. `--allow-republish` overrides it — the dev
   loop, where you rebuild the same version all afternoon.

A published release is **inert**: no device is offered it until a rollout (or a pin)
points at it.

---

*[← 14 · Recovery](14-recovery.md) · [Index](00-introduction.md) · [16 · Cohorts and rollouts →](16-cohorts-and-rollouts.md)*

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
$ openmv-ota client login
token: <paste it; nothing is echoed>
saved /home/you/.config/openmv-ota/client.toml

$ openmv-ota client logout
removed /home/you/.config/openmv-ota/client.toml
```

At a terminal `login` prompts for the token and hides what you paste, so the secret
never lands in shell history. It can also arrive as `--token`, on stdin (a pipe), or from
`OPENMV_OTA_TOKEN`. Every verb resolves its credentials the same way:

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
   refuses an upload whose artifacts don't match it: an artifact whose sha256 or size
   disagrees, a declared delta that didn't arrive, an extra delta the manifest never
   named, a delta whose target size is wrong.

   The artifacts are [encrypted](08-release-artifacts.md#encryption), so what the server
   checks them against is the **ciphertext** digest the signed manifest carries — the
   only one it can, having no key. It also refuses an artifact that could not decrypt at
   all (a length that is not whole blocks, a declared plaintext length that does not fit),
   so a release nobody could install fails at publish rather than in the field. The
   plaintext digest is verified on the camera, which is the only party that can.
4. Publish-time anti-rollback: a `payload_version` at or below the newest already
   published for that product is refused. `--allow-republish` overrides it — the dev
   loop, where you rebuild the same version all afternoon. A project in platform mode
   also stamps a `publish_seq`, and one at or below that product's newest is refused
   with no override: each build takes its own from the server, and reusing one would
   leave two artifacts a camera cannot order.

A published release is **inert**: no device is offered it until a rollout (or a pin)
points at it.

---

*[← 14 · Recovery](14-recovery.md) · [Index](00-introduction.md) · [16 · Cohorts and rollouts →](16-cohorts-and-rollouts.md)*

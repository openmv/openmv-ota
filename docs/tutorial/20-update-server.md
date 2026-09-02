# The update server

*[← 19 · Accounts and tokens](19-accounts-and-tokens.md) · [Index](00-introduction.md) · [21 · Self-hosting →](21-self-hosting.md)*

---

The update server is the central service the `client` verb drives: it hosts the releases
you publish, decides which camera is offered what, and records what the fleet did about
it. Two deployment shapes run the **same software**:

- **OpenMV-hosted (the default):** OpenMV runs the server + website — there is nothing
  to deploy, and everything the earlier pages did happened against it.
- **Self-hosted:** you run your own; the next page is the manual.

Either way, this page is what the thing actually *does* — worth reading even if you
never deploy one, because it is the other half of every `client` command.

## Two things the server never does

**It never holds a signing key.** Releases are signed *locally* by `build ota-romfs` (the
private keys never leave your build host), and the device verifies the signed manifest
against the keys baked into its firmware. The server only stores and distributes
already-signed bytes and runs rollout *policy*. A fully compromised server cannot forge
an update a device will accept — the worst it can do is serve stale bytes or nothing.
Because the manifest's artifact URLs are **relative filenames**, the server serves the
manifest untouched and co-locates the `-ota.img.gz`/`-ota.delta.gz` beside it — no
rewriting, no re-signing.

**It never serves an unregistered device.** Every deployment validates each camera
against OpenMV's central registration server, using what every check-in already carries:
the **board name** (`OPENMV_N6`, …) and the **device id** (the MCU's unique hardware id).
An unregistered pair gets `{update: false}` and **zero stored state** — no device row, no
telemetry, no cache entry — so unknown ids can never grow the database or storage.
Registration is required: the two registration settings below carry the verify endpoint
and an OpenMV-issued token tied to your account.

## What one check-in does

The heart of the server is `POST /api/v1/check` — what every camera calls on its poll
interval. In order:

1. **Rate limit** per client IP (429 with a retry hint when exceeded).
2. **Registration gate** — unregistered pairs are answered `{update: false}` and leave
   nothing behind. A short list of board types the registry structurally never
   registers bypasses the check and is served OTA **read-only**: offers work, but no
   device row is written, so a fake id still can't grow the database. A deployment
   with **no registration server attached at all** — a self-host that can't reach
   OpenMV's — serves *every* device that same read-only way: updates flow, nothing is
   logged. Data collection is the gate's privilege, because without the gate every
   stored row would be attacker-growable.
3. **Account binding** — the device's account is learned from its first valid check-in
   and **sticky** from then on (an admin `bind` overrides it). A later boot that reports
   a different or empty account — a factory-state fallback image, say — can't strand the
   device in the wrong tenant.
4. **The offer decision** — a device **pin** wins, then a **cohort pin**, then the active
   rollout for the device's cohort. Whatever the source, an offer only happens when it's
   an *upgrade* over what the device reports running, the device is *settled* (not
   mid-trial — its fallback slot is worth more than a new download), and — for a
   rollout — the device's stable hash falls inside the current percent.
5. **Rollout accounting** — the check-in feeds the rollout's counters: newly offered
   devices bump `attempted`, a device now running the offered release bumps `updated`,
   and a device transitioning into a fallback bumps `failures`. When the failure rate
   among offered devices crosses the rollout's threshold, the rollout **auto-pauses**
   and the audit log records it.
6. **The device row** — version, slot, confirmation state, fallback identity, cohort —
   is upserted; this is what `client fleet` and `client device list` summarize.
7. **The answer** — `{update: false, poll_after_s: …}` in the common case; on an offer,
   a short-lived download URL for the release's manifest. Where the deployment is wired
   for them, the answer also carries per-device **grants** for OpenMV's live-viewing and
   data-ingest services (their settings are a self-hosting concern).

Downloads then go through the **capability gateway**: the offer's URL embeds an
unguessable, expiring token that authorizes the whole bundle — the manifest and every
image/delta beside it. Devices report their terminal outcome (`installed` / `failed`)
back explicitly, which is what the rollout status counts as `reported`.

## See also

- [Threat model](../reference/threat-model.md) — the trust root and why the server never
  holds a key.

---

*[← 19 · Accounts and tokens](19-accounts-and-tokens.md) · [Index](00-introduction.md) · [21 · Self-hosting →](21-self-hosting.md)*

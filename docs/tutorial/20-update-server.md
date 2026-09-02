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

**It never stores data for an unvalidated device.** A deployment attached to OpenMV's
central registration server validates each camera using what every check-in already
carries — the **board name** (`OPENMV_N6`, …) and the **device id** (the MCU's unique
hardware id) — and an unregistered pair gets `{update: false}` and **zero stored
state**: no device row, no telemetry, no cache entry. A deployment with no registration
server attached serves updates but stores nothing for *any* device. Either way, an
attacker-controlled id can never grow the database or storage.

## What publishing stores

When `client release publish` uploads a release, the server does exactly three things:

1. **Reads the signed manifest** and derives every piece of metadata from it — product,
   version, account, sizes, hashes, which delta files belong — refusing any upload
   whose artifacts don't match what the manifest declares.
2. **Puts the bytes in object storage**, untouched, under a freshly minted release id:
   the manifest at `manifests/<release_id>/manifest.bin`, and the image and each delta
   at `artifacts/<release_id>/<filename>` — *filename* being exactly what the signed
   manifest declares, because that name is how a device will later ask for it.
3. **Records a release row** in the database: the id, the identity fields, the
   representation list, and the storage keys above. (The keys are names *inside the
   bucket* — a device never sees or constructs them; the URL a device gets is the
   gateway's, below.)

That's all — no unpacking, no re-signing, no transformation. The row is the join point
for everything after: rollouts and pins reference the release id, and the download
gateway resolves the id back to those storage keys. Until a rollout or pin points at
the id, the release just sits there.

## What one check-in does

The heart of the server is `POST /api/v1/check` — what every camera calls on its poll
interval. In order:

1. **Rate limit** per client IP (429 with a retry hint when exceeded).
2. **Registration gate** — unregistered pairs are answered `{update: false}` and leave
   nothing behind. The registry itself flags the board types it structurally never
   registers, and those are served OTA **read-only**: offers work, but no device row
   is written, so a fake id still can't grow the database. A deployment
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
   data-ingest services.

## From offer to installed bytes

When a check-in's offer decision lands on a release (step 7 above), its stored bytes
become a download like this:

1. **The offer is one URL.** The check-in answer carries
   `manifest_url: …/d/<token>/manifest.bin`. The `<token>` is a signed, expiring
   **capability**: an HMAC over the release id and an expiry, minted with the server's
   `capability_secret`. It is the entire authorization — no account, no session, no
   device credential — which is safe because it names exactly one release, expires
   (`capability_ttl`, an hour by default), and is only ever handed to a device the
   policy chose.
2. **The device fetches the manifest first** through that URL. The gateway verifies
   the token by recomputing the HMAC — no database lookup — and serves the signed
   manifest byte-for-byte as published. The device checks the manifest's signature
   against its firmware-baked keys *before* anything is erased.
3. **The device picks its representation** from the manifest: the delta whose base
   matches the exact bytes it is running, else the full image.
4. **One token covers the whole bundle.** The manifest's artifact URLs are relative
   filenames, so the chosen file resolves under the same `/d/<token>/` prefix. The
   gateway maps token → release id → the release row → the
   `artifacts/<release_id>/<filename>` key publish stored — which is why a filename
   must match something the signed manifest declares (the token can't fish for other
   objects).
5. **The bytes stream from storage.** The device asked the *server* — but on
   s3-backed deployments the gateway answers with a `302` to a short-lived
   **presigned URL**, and the camera's HTTPS client — the installer — follows it to
   pull the bytes **directly from object storage**, verifying the storage host's
   certificate against the same trust store as every other connection (a self-host
   that pinned only its own server's root must make sure the storage host's root is
   in there too). So authorization and resolution pass through the server on every
   fetch, while the multi-megabyte transfer never does. (On the local-disk backend
   there is no redirect; the server streams the bytes itself.)
   A device on a poor link resumes from the byte offset it reached instead of
   restarting.
6. **The device reports the outcome** — `installed` or `failed` — which is what a
   rollout's status counts as `reported`.

The wire-level shapes of each request live with the device API pages.

## See also

- [Threat model](../reference/threat-model.md) — the trust root and why the server never
  holds a key.

---

*[← 19 · Accounts and tokens](19-accounts-and-tokens.md) · [Index](00-introduction.md) · [21 · Self-hosting →](21-self-hosting.md)*

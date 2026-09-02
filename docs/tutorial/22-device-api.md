# The device API

*[← 21 · Self-hosting](21-self-hosting.md) · [Index](00-introduction.md) · [23 · The admin API →](23-admin-api.md)*

---

What a camera actually speaks to the server: three endpoints, no credentials. Device
calls are rate-limited per IP and gated by device registration, and downloads are
authorized by expiring capability tokens — nothing here can be used to write anything.


**`POST /api/v1/check`** — the check-in. The device runtime sends its identity and state:

| field | meaning |
|---|---|
| `device_id`, `board` | the registration pair — the MCU's unique id + the board name |
| `product_id`, `account_id` | which product/tenant this camera belongs to (baked into its firmware) |
| `app_version`, `payload_version` | the running release, twice: the human string ("1.2.0") and its packed numeric form, which is what the machines compare (the offer gate, anti-rollback). You never set or interpret the number — read the string |
| `slot`, `representation`, `confirmed`, `fallback_reason` | trial state: which slot booted, how it was installed, whether it confirmed, why the other slot was rejected (if it was) |
| `slots` | the device's own account of both flash slots, newest first (detailed below) |
| `streams` | the live image stream names this camera publishes (a multi-camera board, or virtual streams an app defines). Names are sanitized and capped; empty means the single default stream `"0"`. Stored on the device row and used to build the Live grant's per-stream URLs |

Each `slots` entry is
`{slot, running, payload_version, counter, pending, confirmed, body_sha256}` — the
trial state of one flash slot. The server reads three things from the list: whether the
device is **settled** (a running slot still `pending` and not `confirmed` is mid-trial,
so offers are deferred — its fallback is worth more than a download), what it would
**fall back to** (the newest non-running slot — the fleet summary's `by_fallback`), and
the running body's **exact bytes** (`body_sha256`, the delta-base identity that
`release bases --fleet` plans against). A single-image device sends no `slots`; every
reader treats the empty list as *unknown*, never as "no fallback".

The answer is `{"update": false, "poll_after_s": 3600}` in the common case. On an offer:

```json
{
  "update": true,
  "manifest_url": "https://ota.cloud.openmv.io/d/<token>/manifest.bin",
  "release_id": "rel_4f9c2a81d06b73ee",
  "poll_after_s": 3600
}
```

Deployments wired for OpenMV's live-viewing or data-ingest services add `live` / `ingest`
grants to the same answer. Over the rate limit, the reply is `429` with a `Retry-After`
header.

**`GET /d/{token}/{filename}`** — the capability gateway. The token is a signed, expiring
credential minted only when a registered device is offered a release, and **one token
authorizes the whole bundle**: the manifest and every image/delta beside it resolve under
the same `/d/{token}/` prefix, which is why the signed manifest's artifact URLs are
relative filenames. A filename must match something the signed manifest declares —
the token can't be used to fish for other stored objects. On s3 storage the response is a
`302` to a short-lived presigned URL (bandwidth offloads to object storage); on local
storage the bytes stream directly, honouring single-range `Range` requests so a device on
a poor link can resume an interrupted download instead of restarting it.

**`POST /api/v1/feedback`** — the explicit terminal outcome of an offered update:
`device_id`, `board`, `product_id`, `release_id`, and `status` (`installed` or `failed`,
optionally a `reason`). Recorded only for registered devices; these reports are the
`reported` counts in a rollout's status.

**`GET /healthz`** — liveness: `{"ok": true}`.

---

*[← 21 · Self-hosting](21-self-hosting.md) · [Index](00-introduction.md) · [23 · The admin API →](23-admin-api.md)*

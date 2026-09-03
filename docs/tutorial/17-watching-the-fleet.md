# Watching the fleet

*[← 16 · Cohorts and rollouts](16-cohorts-and-rollouts.md) · [Index](00-introduction.md) · [18 · Building deltas →](18-building-deltas.md)*

---

You can't steer what you can't see. Four reads cover the whole picture — each prints
the server's JSON verbatim, so what you see below is exactly what your scripts consume.


## The fleet summary

`client fleet` is the dashboard read: the account-wide alarms on top, and under
`products`, one complete breakdown per product — because version strings, fallbacks,
and cohort composition only mean anything *within* a product:

```
$ openmv-ota client fleet
{
  "total": 950,
  "fell_back": 2,
  "unconfirmed": 9,
  "products": {
    "396486252": {
      "total": 412,
      "by_version": { "1.2.0": 361, "1.1.0": 51 },
      "by_fallback": { "1.1.0": 358, "unknown": 54 },
      "by_cohort": { "__default__": 404, "beta": 8 },
      "fell_back": 2,
      "unconfirmed": 7
    },
    "646934278": {
      "total": 538,
      "by_version": { "3.0.1": 538 },
      "by_fallback": { "3.0.0": 538 },
      "by_cohort": { "__default__": 530, "beta": 8 },
      "fell_back": 0,
      "unconfirmed": 2
    }
  }
}
```

| per-product field | what it answers |
|---|---|
| `by_version` | what this product's devices are running |
| `by_fallback` | what they would fall back **to**. A fleet whose devices all have the previous release behind them is in a very different position from one where half report `unknown` — and that is invisible in `by_version` |
| `by_cohort` | how the product's devices are grouped |
| `fell_back` | devices whose last boot rejected a slot — the direct rollout alarm |
| `unconfirmed` | devices mid-trial. They are also the devices deferring further updates until they settle |

`unknown` in `by_fallback` is a device that did not say — a single-image board, which
has no fallback by design. `--product-id` narrows `products` to one entry, and
`--cohort` scopes every number to that cohort — `client fleet --cohort beta` is the
dashboard for exactly the audiences your `beta` rollouts reach.

## The per-device rows

`client device list` is the same picture one camera at a time — everything the device
reported at its last check-in, plus what the server decided about it:

```
$ openmv-ota client device list --cohort beta --limit 1
{
  "devices": [
    {
      "device_id": "30003d000851303436313832",
      "product_id": 396486252,
      "board": "OPENMV_N6",
      "cohort": "beta",
      "current_version": "1.2.0",
      "current_payload_version": 16908288,
      "slot": "A",
      "representation": "ocdl",
      "fallback_reason": null,
      "confirmed": 1,
      "last_offered_release_id": "rel_4f9c2a81d06b73ee",
      "registrar_ref": null,
      "first_seen": "2026-07-02T09:14:55.310221+00:00",
      "last_seen": "2026-08-31T19:38:47.104563+00:00",
      "pinned_release_id": null,
      "account_id": "acct_7bd21c50e83a94f1",
      "streams": "",
      "fallback_payload_version": 16842752,
      "body_sha256": "9c2ff6a1c4f0f9be55b9e9c25e1e6cf1d5f4f34eaa1f5f5f4b2c9c25e1e6cf1d",
      "fallback_version": "1.1.0"
    }
  ],
  "total": 412
}
```

Versions appear twice on purpose: the packed number is what the device reports and what
comparisons use; the decoded form (`current_version`, `fallback_version`) is for you.
Filters: `--product-id`, `--cohort`; pages with `--limit`/`--offset`, and `total` always
counts the whole scoped fleet so a full page is distinguishable from a complete list.
One camera by id, same row shape:

```
openmv-ota client device show --device-id 30003d000851303436313832
```

## The publish history

`client release list` lists what has been published, newest first — the release rows every
rollout and pin points into:

```
$ openmv-ota client release list --limit 1
{
  "releases": [
    {
      "release_id": "rel_4f9c2a81d06b73ee",
      "product_id": 396486252,
      "product": "my-product",
      "version": "1.2.0",
      "payload_version": 16908288,
      "min_platform_version": 84017152,
      "image_sha256": "5d41402abc4b2a76b9719d911017c592a9c25e1e6cf1d5f4f34eaa1f5f5f4b2c",
      "image_size": 1467392,
      "representations": [
        { "format": "full", "url": "OPENMV_N6-ota.img.gz", "size": 734208 },
        { "format": "ocdl", "url": "OPENMV_N6-ota.delta-1.1.0.gz", "size": 51712,
          "base_payload_version": 16842752 }
      ],
      "manifest_key": "manifests/rel_4f9c2a81d06b73ee/manifest.bin",
      "image_key": "artifacts/rel_4f9c2a81d06b73ee/OPENMV_N6-ota.img.gz",
      "delta_key": null,
      "key_id": 256,
      "uploaded_by": "ci",
      "uploaded_at": "2026-08-31T18:11:59.771402+00:00",
      "account_id": "acct_7bd21c50e83a94f1",
      "dev": 0,
      "sbom_key": "sbom/rel_4f9c2a81d06b73ee/sbom.cdx.json"
    }
  ],
  "total": 9
}
```

Worth knowing in there: `dev` marks a release signed with a throwaway `--dev` key
(provenance, visible forever), and `representations` is the signed manifest's own list —
the deltas a device can choose from, each naming the base it patches.

One release by id, and the evidence that shipped with it:

```
$ openmv-ota client release show --release-id rel_4f9c2a81d06b73ee     # the same row, singly

$ openmv-ota client release sbom --release-id rel_4f9c2a81d06b73ee -o sbom.cdx.json
saved sbom.cdx.json (48213 bytes)
```

`release sbom` hands back the CycloneDX SBOM exactly as publish uploaded it — pipe it
to a scanner (no `-o` writes it to stdout) to answer "does the release the fleet runs
carry this CVE?".

`release artifact` downloads any single artifact by the filename the manifest
declares (the full image or one delta), so you can inspect or re-verify exactly what
devices receive:

```
$ openmv-ota client release artifact --release-id rel_4f9c2a81d06b73ee --filename OPENMV_N6-ota.delta-1.3.0.gz
saved OPENMV_N6-ota.delta-1.3.0.gz (183214 bytes)
```

## The audit log

`client audit` is the append-only record of every admin action — publish, rollout
changes (auto-pauses included), pins, assignments, renames, binds, account and token
events — each with its actor, and each entry hash-chained to the one before, so
tampering with history is detectable:

```
$ openmv-ota client audit --since 41
{
  "events": [
    {
      "seq": 42,
      "ts": "2026-08-31T19:40:11.905122+00:00",
      "actor": "system",
      "action": "rollout.autopause",
      "entity_type": "rollout",
      "entity_id": "ro_1c3f88ba90d2e644",
      "data": { "failures": 3, "attempted": 41 },
      "prev_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "entry_hash": "2c26b46b68ffc68ff99b453c1d30413413422d706483bfa0f98a5e886266e7ae",
      "account_id": "acct_7bd21c50e83a94f1"
    }
  ]
}
```

It pages by `--since SEQ` — a sequence **cursor**, not an offset — so a poller resumes
exactly where it left off and never skips or repeats entries when new ones land
mid-page.

## Scripting: `--json`

Every verb takes `--json`, which prints the server's response verbatim instead of the
summary line — so nothing has to be scripted by parsing English:

```bash
rel=$(openmv-ota client release publish ./p -b OPENMV_N6 --json | jq -r .release_id)
tok=$(openmv-ota client token issue --account-id "$acct" --name ci --json | jq -r .token)
```

Verbatim matters most for the one-time secrets (`account create`, `token issue`,
`token rotate`): the token exists in exactly that one response, and a script that can't
capture it has to mint another. `publish --percent` is two API calls, so its JSON nests
the rollout under `rollout` and leaves the release fields where a plain `publish` puts
them — no special case for parsers.

---

*[← 16 · Cohorts and rollouts](16-cohorts-and-rollouts.md) · [Index](00-introduction.md) · [18 · Building deltas →](18-building-deltas.md)*

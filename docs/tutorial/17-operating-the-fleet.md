# Operating the fleet

*[← 16 · Cohorts and rollouts](16-cohorts-and-rollouts.md) · [Index](00-introduction.md) · [18 · Watching the fleet →](18-watching-the-fleet.md)*

---

Publishing is a moment; operating is the rest of the time. Two jobs recur for as long
as a fleet is live: the **exceptions** — devices that must hold a release or jump to
one regardless of any rollout — and the **next release**, whose deltas must match what
the field is actually running today. This page is those levers.

## Pins

A pin overrides rollouts for one device or one whole cohort — "this camera runs exactly
this release":

```
openmv-ota client device pin --device-id 30003d000851303436313832 --release-id rel_4f9c2a81d06b73ee
openmv-ota client device pin --device-id 30003d000851303436313832 --clear
openmv-ota client cohort pin --product-id 396486252 --cohort beta --release-id rel_4f9c2a81d06b73ee
```

A device pin beats a cohort pin, and either beats the rollout. A pin only ever produces
an **offer** when it's an upgrade for a settled device; pinning to the version a camera
already runs (or older) simply holds it — no rollout reaches it, nothing downgrades.

`cohort pin` names the product because a cohort can hold devices of **several
products** — your `OPENMV_N6`s and `OPENMV_RT1060`s can all carry the label `beta` —
while the pin carries a release, and a release only fits one product. So pinning
`--product-id 396486252 --cohort beta` freezes only beta's N6 devices; beta's RT1060s
keep following their own rollouts until you pin them too, with the RT release. (Rollouts
work the same way — targeting is always the `(product, cohort)` pair.) A device pin
needs no product: the device id alone is unique.

## Building the next release's deltas

A device patches against **the release it is running**, so a fleet mid-rollout is
spread over several versions, and one delta reaches only the devices on its base. The
deltas must be built **locally** — a delta is named in the *signed* manifest, and the
server never holds signing keys — but only the server knows what the fleet is actually
running. So the release build asks it:

The client asks and fetches, the build stays local — `build` never talks to the
server:

```
$ openmv-ota client release bases --fleet -b OPENMV_N6 --product-id 396486252 -o build/bases
build/bases/OPENMV_N6-base-1.1.0.img.gz  (1.1.0, 358 device(s), 731648 bytes)
build/bases/OPENMV_N6-base-1.0.0.img.gz  (1.0.0, 51 device(s), 730112 bytes)

$ openmv-ota build ota-romfs . --delta-from build/bases   # one delta per fetched base
$ openmv-ota client release publish . -b OPENMV_N6        # uploads the image + every delta
```

`--fleet` warns at fetch time about any group of devices no base can cover (a pruned
release, a republish that split the bytes) — those take the full image, never nothing.
The server can answer because it **retains every published image**: lose the build
directory, re-clone the repo, hand the release to a colleague — the bases are still
there. (`release bases --last N` fetches the N most recent releases instead, when you
want an old image itself rather than the fleet's plan.)

**The first base is the factory image.** A fresh-from-manufacture fleet runs bytes
that were never published, so its first OTA could never be a delta — which is why
`build factory-romfs` also renders its exact bytes in release form,
`build/factory/<board>-ota.img.gz` plus a factory-key-signed manifest. Publish that
pair right after manufacture and the first update ships as a delta too:

```
openmv-ota client release publish . -b OPENMV_N6 -o build/factory
```

(A local build already defaults its delta base to the recorded factory image;
publishing it is what lets `release bases --fleet` cover factory-fresh
devices as well.)

Retention has **no depth limit**: images are small, and only you know how long a version
stays in the field. Reclaiming space is therefore a deliberate act:

```
openmv-ota client release prune --release-id rel_4f9c2a81d06b73ee
```

The release **row** survives — it is the audit trail and the anti-rollback history — so
the server can answer "this release existed but its bytes are gone" rather than a bare
not-found. Pruning is refused while a rollout still offers that release (those are the
devices downloading it right now): pause or stop it first, or pass `--force` if you
mean it.

---

*[← 16 · Cohorts and rollouts](16-cohorts-and-rollouts.md) · [Index](00-introduction.md) · [18 · Watching the fleet →](18-watching-the-fleet.md)*

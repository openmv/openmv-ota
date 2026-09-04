# Building deltas

*[← 17 · Watching the fleet](17-watching-the-fleet.md) · [Index](00-introduction.md) · [19 · Accounts and tokens →](19-accounts-and-tokens.md)*

---

The fleet is moving, and the next release is coming. Shipping it small — as deltas —
means building against what the field is *actually running today*, which only the
server knows. This page is that loop: fetch the bases, build, publish.


A device patches against **the release it is running**, so a fleet mid-rollout is
spread over several versions, and one delta reaches only the devices on its base. The
deltas must be built **locally** — a delta is named in the *signed* manifest, and the
server never holds signing keys. So the client asks and fetches, and the build stays
local (`build` never talks to the server):

```
$ openmv-ota client release bases --fleet -b OPENMV_N6 --product-id 396486252 -o build/bases
build/bases/OPENMV_N6-base-1.1.0.img.gz  (1.1.0, 358 device(s), 731648 bytes)
build/bases/OPENMV_N6-base-1.0.0.img.gz  (1.0.0, 51 device(s), 730112 bytes)

$ openmv-ota build ota-romfs . --delta-from build/bases   # one delta per fetched base
$ openmv-ota client release publish . -b OPENMV_N6        # uploads the image + every delta
```

`--fleet` warns at fetch time about any group of devices no base can cover (a version
never published through the server, a republish that split the bytes) — those take the
full image, never nothing.
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

Retention has **no depth limit, and no delete**: the published bytes are part of a
release's history — the SBOM and the manifest hash are testimony about the image,
but the image itself is the evidence — so the server keeps every artifact for as
long as the release row exists. Images are small; only you know how long a version
stays in the field. (A release that must stop reaching devices is a rollout
question — stop its rollouts — not a deletion question.)

---

*[← 17 · Watching the fleet](17-watching-the-fleet.md) · [Index](00-introduction.md) · [19 · Accounts and tokens →](19-accounts-and-tokens.md)*

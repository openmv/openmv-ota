# Integrating as a platform

*[← 24 · Pulling device data](24-pulling-device-data.md) · [Index](00-introduction.md)*

---

Every page before this one assumed you are the operator of your own fleet. This one is
for a **platform**: a product with its own customers, its own accounts, and its own UI,
that runs OpenMV cameras underneath and drives this service entirely through the API.
Nobody on your side logs into the OpenMV website. Your server holds the credentials,
your customers see your product, and the update service is plumbing.

It is written to be read start to finish before you write anything, because three of the
decisions here are hard to reverse once you have fielded cameras.

## What you are mapping onto

Four nouns, and which of yours goes where is the first decision:

| Ours | What it is | Changing it later |
|---|---|---|
| **account** | the tenancy boundary. Releases, devices, cohorts, rollouts, audit — all namespaced by it. One account can never see another, and a cross-account lookup answers 404 rather than 403 | hard: devices rebind, releases do not move |
| **product** | a line of firmware. Its id is the low 63 bits of `sha256("<product>:<board>")`, computed by your project config, and it is the device's cross-flash guard — a camera refuses an image whose product id is not its own | hard: it is baked into installed images |
| **device** | one camera. Its id is `BOARD:<unique-id>` — board-qualified, because `machine.unique_id()` is only unique among boards of the same type | n/a |
| **cohort** | a label on devices within an account (`beta`, `us-east`, a customer). Rollouts and pins target one | easy: it is a string on a row |

Note what a product is **not**: it is not one per customer unless you build one image per
customer. The id is derived from the project name and the board, so a single project that
supports four boards is **four product ids**, and every camera of a given board across
every customer running that image shares one.

## The two shapes, and which one you want

Most platforms arrive with one master project — a runtime that loads a workload, rather
than a separate firmware per customer. That leaves two ways to slice it.

### A · An account per customer

The one to pick if your customers' data must not mingle, or if you ever want to hand a
customer a credential of their own.

```
your platform ──(operator token)──> account "Acme"    ──> products {runner:N6, runner:RT1060, …}
                                     account "Globex"  ──> the same product ids, its own devices
```

Isolation is total and it is the boundary the server is built around. The cost is real
and you should price it in now: **a release belongs to an account**, so publishing your
master project to fifty customers is fifty publishes of the same artifacts. That is fifty
`POST /releases` calls and fifty copies in storage. The build happens once on your server;
only the upload repeats.

### B · One account, a cohort per customer

One publish, then `cohort assign` each customer's devices and pin or roll out per cohort.
Cheap, and the rollout machinery already does exactly this.

What you give up: there is no boundary between your customers inside that account. A
credential for it sees every device you operate. Do not hand one to a customer, and be
aware that your own bugs are not contained by anything.

**Pick A unless you are certain no customer will ever need visibility.** Moving from B to
A later means rebinding every device and republishing every release.

### The third option, when a customer really does need a login

Within one account, a token can be limited to a subset of products
(`products: [<id>, …]` when you issue it). That credential sees those products' releases,
devices, rollouts and audit rows, and nothing else in the account. It is the right tool
when a customer wants read-only visibility into their own line — but it slices by
*product*, not by customer, so it only helps if each customer has their own product, which
brings you back to one image per customer.

## Credentials

Three kinds. There is **no impersonation** — a token's account comes from the token, and
there is no "act as" header — so your server holds one credential per account and picks
the right one per call.

| Token | Scope | What it does |
|---|---|---|
| your operator token | `accounts` | creates accounts, mints their tokens, sets device limits, deactivates |
| an account's token | `publish` > `manage` > `observe` | everything inside one account |
| a limited token | any of the above, plus `products: [...]` | the same, confined to some products |

`accounts` lets you provision customers **and see only the ones you provisioned**. The
wider `accounts.all` — the server's own root — sees every account on the server, and you
will not be issued one. If `GET /accounts` returns accounts you did not create, you are
holding the wrong token; say so.

Store account tokens the way you store any customer secret. They are returned **once**,
at creation, and only their hash is kept — a lost token is rotated, never recovered.

## Provisioning a customer

```bash
openmv-ota client account create --name "Acme Robotics" --client-ref "ws_8a41f2" --json
```

`--client-ref` is your own id for the account, and it is what makes this call **safe to
retry**. Call it again with the same reference and you get the same account back with
`"created": false` and `"token": null` — not a second account, and not a 409 you cannot
tell apart from someone else owning the name. Use your workspace/tenant id. Without it, a
timeout leaves you unable to tell whether the account exists.

Account names are unique **within your operator**, so your customer called "Acme" does not
collide with anyone else's.

Then set the entitlement and mint the credentials your server will use:

```bash
openmv-ota client account limit --account-id acct_… --devices 250
openmv-ota client token issue --account-id acct_… --name "platform-publish" --scope publish
openmv-ota client token issue --account-id acct_… --name "platform-read"    --scope observe
```

The device limit is enforced at check-in for **new** devices only; cameras already
registered are never dropped when a plan shrinks. A refusal lands in the audit log as
`device.refused`, once per device id — poll for it if you want to surface "you are at your
limit" in your own UI.

## Declaring the product, before there is an image

```bash
openmv-ota client product create --product-id 4242 --name "Workflow runner (N6)"
```

The id comes from your project's `ota.toml`, where it was computed — the server does not
derive it, so your project stays the one place a product is named. Declaring is
idempotent, and it exists so you can create the project, name it, and bind its first
cameras before you have built anything. Do this once per board you support, per account.

## Binding installs

A camera that checks in learns its account. You usually want to decide it instead:

```bash
openmv-ota client device bind --device-id OPENMV_N6:3c0021000c51
```

This works **before the camera has ever checked in** — an admin bind always wins over a
learned one, so you can register an install at the moment you ship the hardware, and the
first check-in lands in the right account. That is the only way to be sure: a camera that
checks in unbound is claimed by whoever it talks to first.

When an install ends:

```bash
openmv-ota client device forget --device-id OPENMV_N6:3c0021000c51
```

The device leaves the fleet and stops consuming the limit. Its **install history stays** —
a deployment row records what happened on a day that has already passed, and rollout
counters are built from those rows. The audit log keeps the removal. A camera that checks
in again afterwards is simply a device the server has not seen: it enrols from scratch,
learns a binding, and is not yours unless you bind it. This call is about the fleet, not
about entitlement.

## Publishing

The build is **not** an API call. You install this package on your server and build there,
because building needs two things the server must never hold:

- the project's **signing key**. The device verifies the signature itself; that is what
  makes an update safe, and the server is not trusted with it. `openmv-ota` ships a
  pluggable signer — an encrypted PEM at minimum, and PKCS#11, AWS/GCP/Azure KMS, or your
  own hook. **Use a KMS.** One key per customer or one key for the platform is your call,
  but a plaintext key on a build box is not.
- the project's **payload keys**, if you want published artifacts encrypted at rest in the
  store (see [page 8](08-release-artifacts.md)). The board key is baked into the firmware
  you build, so key material and image are made together.

Then publish per account, with that account's `publish` token:

```bash
openmv-ota client release publish ./projects/runner -b OPENMV_N6
```

which uploads what you built for that board, under whichever account token is in the
environment. Loop it over your accounts, changing only the credential.

Publishing is also what a product's *name* comes from, if you never declared one.

### What your workload is, and what it costs you

If a customer's workload ships **as the image** — a Python app baked into the ROMFS — then
every workload change is a release, a rollout, and a reboot into the new slot. Correct,
atomic, rollback-protected, and heavier than a config change should be.

If the workload is **data the image loads** — a model and settings your runtime fetches —
then the image changes rarely and workload updates are your own traffic, not ours. That
keeps this service for what it is good at: shipping the runtime, safely, with rollback.

Either works. The second means far fewer releases, and it moves the "did the workload
apply?" question into your system, where you have better answers than a check-in can give.

## Driving updates

Per account, with its `manage` token. All of it is on [page 16](16-cohorts-and-rollouts.md);
the platform-specific notes:

- **`cohort assign`** takes a list of device ids or a whole product. A cohort is just a
  label, so it is where your own grouping goes — a site, a tier, a canary set.
- **`rollout create --percent 10`**, then raise it. A rollout auto-pauses when failures
  cross its threshold, with `pause_reason: "failure_limit"` — that is the field your
  dashboard's "needs attention" should watch.
- **`device pin`** beats a cohort pin, which beats a rollout. Pin the one unit a customer
  is mid-incident with; do not pin fleets.

## Live video

`POST /api/v1/admin/devices/{device_id}/viewer-grant` with the account's `observe` token
returns a short-lived viewer credential and ready-made URLs. This endpoint exists for
exactly your case: **you authenticate your own user however you like, then mint a grant
and hand it to that user's browser.** The signing secret never leaves the OTA server, and
the grant is scoped to one device and expires in minutes.

The relay is a WebSocket. From a browser, the credential goes in the subprotocol; from
your server, in a header:

```
new WebSocket(url, ["openmv.bearer", token])       // browser
Authorization: Bearer <token>                       // your server
```

The `?token=` form in the URLs still works and has to — fielded cameras run firmware that
sends it — but do not build new URLs that way. A credential in a URL lands in proxy logs,
browser history and `Referer` headers.

Grants are minted per device and expire in minutes: **mint one per view, not one per
month**, and do not cache them across users. There is no batch grant endpoint; if you are
opening a hundred tiles, that is a hundred calls.

## Data

`POST .../devices/{id}/viewer-grant` also carries the datalake half, and
`POST .../products/{product_id}/viewer-grant` gives you one credential for a whole
product's devices together. Read it as described on [page 24](24-pulling-device-data.md).

Two shapes, and the difference matters when you plan:

- **`logs/{topic}`** returns records with a `before_seq` cursor — a real backfill. Page it
  and you have every line.
- **`series/{topic}`** returns **aggregated buckets** (`t`, `n`, `min`, `max`, `avg`), not
  samples. There is no raw-sample export for numeric telemetry. If your product needs
  sample-level data, do not plan around pulling it out of here — have the device send it
  where you want it, or tell us and we will talk about an export endpoint.

## Watching, without webhooks

There are none. Poll, with cursors — they are there and they are cheap:

| Want | Call |
|---|---|
| everything that happened, in order | `GET /audit?since=<last_seq>` — append order, `seq` is the cursor |
| devices that checked in recently | `GET /devices?seen_since=<epoch>` |
| devices that have not | `GET /devices?not_seen_since=<epoch>` |
| what is behind | `GET /devices?behind=true`, or `?older_than_release=<id>` |
| whether installs are landing | `GET /fleet/installs?days=14` |

`/audit?since=` is the one to build on: it is an append-only log with a monotonic
sequence, so a poller that remembers the last `seq` it saw never misses an event and never
sees one twice. A product-limited token reads its own products' history; an account token
reads the account's.

The admin API is **not** rate limited (only the device check-in edge is), so poll at a
sensible interval rather than a fearful one.

## Failure modes worth handling on day one

- **A 404 usually means "not yours"**, not "does not exist". The API refuses to confirm
  existence across a boundary. If a device id you believe you own 404s, check which
  account's token you used before you check your database.
- **A 409 on account create** means the name is taken *among your own accounts*. With a
  `--client-ref` you should never see it; without one, you cannot distinguish it from a
  retry that already succeeded.
- **The account token is returned once.** If your provisioning transaction fails after the
  create call, you have an account you cannot use — recover with
  `client account create --client-ref <same>` to identify it, then
  `client token issue` to mint a fresh credential.
- **Device ids are board-qualified.** `OPENMV_N6:3c0021000c51`, not `3c0021000c51`. An id
  from a sticker or a serial-number system needs the board prefix before it will match.
- **Product ids are 63-bit integers.** JSON numbers are doubles in JavaScript and lose
  precision above 2^53 — every response that carries one also carries `product_id_str`.
  Read that one from JS.

## The short version

1. One account per customer, created with a `--client-ref` you can retry on.
2. One product id per board of your master project, declared up front.
3. Bind each install when you ship it; forget it when it ends.
4. Build and sign on your own server, with a KMS; publish per account.
5. Cohorts for grouping, rollouts for staging, pins for exceptions.
6. Viewer grants for video and data, minted per view.
7. Poll `/audit?since=` for everything else.

---

*[← 24 · Pulling device data](24-pulling-device-data.md) · [Index](00-introduction.md)*

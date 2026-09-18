# Integrating as a platform

*[← 24 · Pulling device data](24-pulling-device-data.md) · [Index](00-introduction.md)*

---

Every page before this one assumed you run your own fleet: one product, your devices,
your releases. This page is about the other case — a **platform**, whose own customers
run the cameras. Your product has its own accounts, its own UI and its own support, and
this service is underneath it. Nobody on your side signs into the OpenMV website; your
server holds the credentials and calls the [admin API](23-admin-api.md), and your
customers never learn it is there.

Nothing on this page is a separate mode. It is the same accounts, products, cohorts and
rollouts as everywhere else, arranged for that shape, plus the three things that only
come up when you are operating on someone else's behalf: carving the account up,
assigning hardware to a customer after it is built, and versioning across all of it.

## One account, products underneath

You get **one account**. It is the tenancy boundary — releases, devices, cohorts,
rollouts and the audit log all live inside it, and nothing crosses between accounts. Your
customers are rows in your own database; this service never models them, authenticates
them or knows they exist. The account's device limit is one number across everything you
operate, and its audit log is the record of everything you did on their behalf.

Inside that account, the **product** is what you carve by. A product id is the low 63
bits of `sha256("<product>:<board>")`, computed by your project config, and it is what a
camera checks an update against before installing it — an image whose product id is not
the device's own is refused on the device, not just on the server.

That gives you two ways to arrange customers, and they differ in how much of the camera's
behaviour you are willing to make configuration.

**A product per end customer.** Build the same project source under a per-customer product
name, so each customer has their own product id on each board they use. Whatever varies
between customers — Python code, a model, thresholds, an entire application — varies in
the image, and you never have to design a settings format that anticipates it. Each
customer gets their own release stream to roll out and pin against, and a credential
limited to their products can be handed over if you ever want them to see their own
fleet. With N customers on M boards that is N×M product ids under one account; the id is
63 bits precisely so a platform can mint them at that rate.

**One product per board.** One image for everyone, with customers separated by
[cohort](16-cohorts-and-rollouts.md), and per-customer behaviour arriving as data your
application fetches at runtime. One build, one publish, one release stream. A cohort is a
label on a device row, so it groups but does not isolate: there is no per-customer
credential, and everything your customers do runs through one application you have to
make configurable enough for all of them.

Most platforms end up mixing the two — a shared runtime for the common case, a dedicated
product for a customer who needs something the runtime cannot express.

## Declaring a product before you build one

A product normally comes into existence when you publish to it. When you are provisioning
in one order and building in another, declare it up front:

```bash
openmv-ota client product create --product-id 4242 --name "Acme Robotics (N6)"
```

The id comes from the project's `ota.toml`, where it was computed — the server does not
derive it, so the project stays the one place a product is named. Declaring is idempotent
and gives you a product that appears in `client product list` with no releases and no
devices, ready to be named and to have cameras bound to it.

## Stock hardware, claimed later

Cameras are usually built before anyone knows which customer will receive them. Give that
hardware a **stock product** of its own, and build its firmware with a product id of `0`:

```toml
[targets.OPENMV_N6]
product_id = 0
```

Zero turns the device's cross-product check off — the installer only compares product ids
when it has one — so a stock unit will accept an image from any product. What it does
**not** turn off is the account check: a stock camera is still confined to the account it
is bound to, so "any product" means any of yours.

Claiming one for a customer is then an ordinary device pin, to a release of a *different*
product:

```bash
openmv-ota client device pin --device-id OPENMV_N6:3c0021000c51 --release-id cust_a_r1
```

The pin checks that the release belongs to the account and that it is an upgrade. It
deliberately does not check that it belongs to the device's current product — which is
what makes one manufactured SKU able to become any customer's product after unboxing. The
device takes it on its next check-in like any other update: downloaded, verified, staged
into the other slot, rolled back if it does not boot.

**Claim, transfer and reset are the same primitive.** Moving a camera from one customer to
another is a pin to the new customer's release. Taking it back is a pin to the current
stock release. There is no separate verb, and no state the server has to keep in step.

One thing to build around: **a pin on a device the server has never seen does nothing.**
The fleet row is created by the first check-in, so a claim issued before a camera has ever
been powered on has nothing to attach to. Either claim at first check-in, or pin and let
your flow tolerate the wait.

## The build counter

This is the one hard requirement of the arrangement, and it is worth getting right before
you ship anything.

**Use a single, globally monotonic counter for `payload_version` across every product in
your account.** Not one sequence per product — one sequence, shared.

The reason is on the device. Its rollback floor rises with every install and is
**product-agnostic**: it records the highest payload version the camera has ever run,
without reference to which product that version belonged to. An offer below the floor is
refused by the firmware itself, and the server's pin is upgrade-only besides. So if each
product has its own independent version sequence, a camera that has run customer A's
build 40 cannot be transferred to customer B whose stream is at 12 — not until B passes
40, and not at all if B never does. The first transfer that crosses a numerically older
stream wedges permanently, in the field, with no way back short of a depot visit.

Your human-facing version numbers are unaffected: those live in your own metadata and in
the release's display name. It is the packed `payload_version` — the number the device
compares — that has to be the shared counter.

De-association follows from the same rule. Returning a camera to stock is a pin to the
**current** stock release, which under one counter is always newer than whatever the
customer was running. It is a forward step, never a downgrade, and it is an ordinary OTA
install rather than a reflash.

The only true as-manufactured reset is `openmv-ota flash factory`, because it is the only
thing that clears the rollback sectors. That needs the camera in hand, so it is a depot
or RMA path, not something a customer does.

One consequence to design for: an OTA reset replaces the application, not the filesystem.
Whatever the previous customer left in `/flash` is still there when the next one powers
the camera on. If your application keeps anything there — credentials, cached data,
captures — clear it on first boot after a reset.

## Binding and unbinding hardware

A camera that checks in unbound is claimed by the account it talks to first, which is
fine when you control the firmware it ships with and wrong when you do not. To decide it
yourself:

```bash
openmv-ota client device bind --device-id OPENMV_N6:3c0021000c51
```

An administrative bind wins over a learned one and works **before the camera has ever
checked in**, so you can register hardware at the moment it leaves your building and the
first check-in lands where you expect.

When a unit is retired:

```bash
openmv-ota client device forget --device-id OPENMV_N6:3c0021000c51
```

It leaves the fleet and stops counting against the account's device limit. Its install
history stays — a deployment row records what happened on a day that has already passed,
and rollout counters are built from those rows — and the removal is in the audit log. A
camera that checks in again afterwards is a device the server has not seen before: it
enrols from scratch and is not yours again until you bind it.

## Building and publishing

The build happens on your machines, not ours, because it needs two things the server must
never hold: the project's **signing key**, which is what the device verifies before it
installs anything, and its **payload keys**, if you are encrypting published artifacts
(see [page 8](08-release-artifacts.md)). Both are made with the project, and the board key
is baked into the firmware you build, so key material and image are produced together.

The signer is pluggable — an encrypted PEM at minimum, and PKCS#11, AWS/GCP/Azure KMS, or
your own hook ([page 5](05-signing-keys.md)). At platform scale, a key in a KMS is worth
the setup: you will be signing unattended, on a schedule, for a long time.

Publishing is the same verb as anywhere:

```bash
openmv-ota client release publish ./projects/acme -b OPENMV_N6
```

With a product per customer this runs once per customer per board, against the same
account and the same shared counter.

## Credentials

Your server holds an account token and uses it for everything. There is no impersonation —
a token's account comes from the token itself, and there is no "act as" header — so if
you are ever issued more than one account, you hold a credential for each and choose per
call.

Scopes are the ladder from [page 19](19-accounts-and-tokens.md): `publish` > `manage` >
`observe`. Give a build pipeline `publish`, a dashboard `observe`, and keep them separate
so a leak from one is not a leak from both.

A token can also be **limited to some products** when it is issued:

```bash
openmv-ota client token issue --account-id acct_… --name "acme-readonly" \
    --scope observe --product-id 4242
```

That credential sees those products' releases, devices, rollouts and audit history, and
nothing else in the account — not the other products, and not the account-level record of
tokens and limits. With a product per customer, it is how a customer gets a view of their
own fleet without a view of everyone else's.

## Live video and device data

`POST /api/v1/admin/devices/{device_id}/viewer-grant`, with an `observe` token, mints a
short-lived credential for one device and returns ready-made URLs. This is the endpoint to
build a customer-facing live view on: **you authenticate your own user however you like,
then mint a grant and hand it to that user's browser.** The signing secret never leaves
the OTA server, and the grant is scoped to one device and expires in minutes, so it is
safe to give out and cannot be recalled — which is why it is short.

The relay is a WebSocket. From a browser the credential rides in the subprotocol; from a
server, in a header:

```js
new WebSocket(url, ["openmv.bearer", token])    // browser
```
```
Authorization: Bearer <token>                    // server to server
```

The `?token=` form in the returned URLs still works, and has to — cameras in the field run
firmware that sends it — but prefer the other two for anything you write. A credential in
a URL ends up in proxy logs, browser history and `Referer` headers.

Grants are per device and expire in minutes: mint one per view rather than caching them,
and note there is no batch endpoint, so a page of a hundred tiles is a hundred calls.

Device data comes through the same grant, or through a product-wide one — the reads are
on [page 24](24-pulling-device-data.md). Two shapes, and the difference matters when you
plan a product around it: `logs/{topic}` returns records with a `before_seq` cursor, so it
backfills completely; `series/{topic}` returns aggregated buckets (`t`, `n`, `min`, `max`,
`avg`) rather than samples. There is no raw-sample export for numeric telemetry.

## Watching a fleet you do not sit in front of

There are no webhooks. Everything is polled, and the reads are built for it:

| To find | Call |
|---|---|
| everything that happened, in order | `GET /audit?since=<last_seq>` |
| cameras that have checked in recently | `GET /devices?seen_since=<epoch>` |
| cameras that have not | `GET /devices?not_seen_since=<epoch>` |
| what is behind | `GET /devices?behind=true` or `?older_than_release=<id>` |
| whether installs are landing | `GET /fleet/installs?days=14` |

`/audit?since=` is the one to build on. It is append-only with a monotonic sequence, so a
poller that remembers the last `seq` it saw never misses an event and never sees one
twice — including the ones you cannot get any other way, such as `device.refused`, which
is written once per device when a camera is turned away for being over the account's
limit. The admin API is not rate limited; only the device check-in edge is.

## If you provision accounts as well

A platform that needs real separation between its customers — separate fleets, separate
audit logs, separate credentials — can be issued an operator credential carrying the
`accounts` scope, and create accounts of its own through the API.

```bash
openmv-ota client account create --name "Acme Robotics" --client-ref "ws_8a41f2" --json
openmv-ota client account limit --account-id acct_… --devices 250
openmv-ota client token issue --account-id acct_… --name "platform" --scope publish
```

`--client-ref` is your own id for the account, and it makes the call safe to retry: asking
again with the same reference returns the account you already made, with
`"created": false` and no token, rather than a second account or a 409 you cannot tell
apart from the name being taken. Account names are unique within your own accounts, not
across the server.

An `accounts` credential sees and manages the accounts **it** created and no others;
another operator's account answers 404, the same as one that does not exist. The account's
first token is returned once, at creation, and only its hash is stored — a lost token is
rotated, never recovered.

Be aware of what this arrangement costs before choosing it: a release belongs to an
account, so one project shipped to fifty customer accounts is fifty publishes of the same
artifacts, and the claim-by-pin primitive above does not apply — a camera moving between
accounts is a rebind, not a pin, and it crosses a boundary the device itself enforces.

---

*[← 24 · Pulling device data](24-pulling-device-data.md) · [Index](00-introduction.md)*

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
rollouts as everywhere else, arranged for that shape, plus the things that only come up
when you are operating on someone else's behalf: carving the account up, assigning
hardware to a customer after it is built, and versioning across all of it.

Two of those decisions are made before the first camera ships and cannot be changed
afterwards. They come first.

## One account, products underneath

You get **one account**. It is the tenancy boundary — releases, devices, cohorts,
rollouts and the audit log all live inside it, and nothing crosses between accounts. Your
customers are rows in your own database; this service never models them, authenticates
them or knows they exist. The account's device limit is one number across everything you
operate, and its audit log is the record of everything you did on their behalf.

Inside that account, the **product** is what you carve by. A product id is the low 63
bits of `sha256("<product>:<board>")`, computed by your project config. For an ordinary
camera it is also the cross-flash guard — the firmware refuses an image whose product id
is not its own. Your cameras will run with that guard off, for reasons the next sections
get to, so for you a product is a way of *organising* releases and devices rather than a
wall between them.

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

## Keys: one set for the whole fleet

This is the first of the two decisions that cannot be changed after the first camera
ships.

A camera verifies an image against the trusted keys **baked into its firmware**, and
decrypts a payload with the payload keys baked in beside them. Firmware is not replaced
over the air, so those are the keys that camera has for its whole life. A stock camera
carries the *stock project's* keys — and it is going to be asked to install customer A's
image, then customer B's.

So **every project a camera can be moved between must share one signing key and one
payload key set.** In practice that means your whole fleet: one signing identity, one
payload key set, reused by every project you create. `project new` mints fresh keys by
default, which is right for a product line and wrong for you. Make your first project
normally, then point every later one at it:

```bash
openmv-ota project new ./projects/stock  -f ../openmv -b OPENMV_N6 --ota \
    --key-passphrase-file ~/.openmv/fleet-passphrase
openmv-ota project new ./projects/acme   -f ../openmv -b OPENMV_N6 --ota \
    --key-passphrase-file ~/.openmv/fleet-passphrase --keys-from ./projects/stock
```

`--keys-from` copies the trusted set, the private keys and the payload keys. The private
keys stay encrypted under the passphrase they were minted with, so it has to be the same
one — that is checked when the project is made, rather than surfacing later as a key file
that will not open. A project that builds for a board the source does not gets a payload
key minted for it and says so; the signing keys are shared untouched.

Two things follow that are worth being explicit about with anyone reviewing this:

- The boundary between your customers is **the server deciding what to offer**, not
  cryptography on the device. Every camera in your fleet can verify and decrypt every
  image in it. That is the same trust domain by construction — they are all your images —
  but it is not a wall, and it should not be described as one.
- A camera built for this arrangement has the cross-flash guard off permanently. What
  protects it is the signature, the account binding, and the publish counter below. That
  is a coherent story; it is just a different one from a normal product's.

The **stock image deserves particular care**, because it is the one artifact every camera
can be returned to. Keep it as close to inert as the job allows — register, check in,
wait — with no customer data handling and as little network surface as you can manage. A
weakness there is a weakness in every camera you have ever shipped.

## Declaring a product before you build one

A product normally comes into existence when you publish to it. When you are provisioning
in one order and building in another, declare it up front:

```bash
openmv-ota client product create --product-id 4242 --name "Acme Robotics (N6)"
```

The id comes from the project's `openmv-ota.toml`, where it was computed — the server does
not derive it, so the project stays the one place a product is named. Declaring is
idempotent and gives you a product that appears in `client product list` with no releases
and no devices, ready to be named and to have cameras bound to it.

## Stock hardware, claimed later

This is the second decision that cannot be changed afterwards.

Cameras are usually built before anyone knows which customer will receive them. Give that
hardware a **stock product** of its own, and build its firmware with a product id of `0`:

```toml
[ota]
platform = true

[targets.OPENMV_N6]
product_id = 0
```

Zero turns the device's cross-product check off — the installer only compares product ids
when it has one — so a stock unit will accept an image from any product. What it does
**not** turn off is the account check: a stock camera is still confined to the account it
is bound to, so "any product" means any of yours.

The build refuses `product_id = 0` unless `platform` is set, and the two really are one
decision: a camera that can change product cannot have its images ordered by a per-product
version, so it needs the publish counter that `platform` turns on. Setting one without the
other is always a mistake. And it is permanent — firmware is not replaced over the air, so
the id a camera leaves the factory with is the id it has forever.

Claiming one for a customer is then an ordinary device pin, to a release of a *different*
product:

```bash
openmv-ota client device pin --device-id OPENMV_N6:3c0021000c51 --release-id cust_a_r1
```

The pin only requires that the release belongs to your account; it deliberately does not
require that it belongs to the device's current product, which is what makes one
manufactured SKU able to become any customer's product after unboxing. The camera is
offered the pinned release on its next check-in if the release moves it forward — by the
publish counter, for these cameras, not by the version — and takes it like any other
update: downloaded, verified, staged into the other slot, rolled back if it does not
boot.

**Claim, transfer and reset are the same primitive.** Moving a camera from one customer to
another is a pin to the new customer's release. Taking it back is a pin to the current
stock release. There is no separate verb, and no state the server has to keep in step.

**A pin does not need the camera to have checked in.** It is an intent about a device id,
not a field on a fleet row, so you can record it when the hardware ships — or the moment a
customer scans a code — and it is waiting on that camera's very first check-in. The claim
lands on first contact rather than on the poll after it, which is the difference between a
customer watching a spinner for one interval and for two. The only thing refused is an id
already bound to another account, which is a 404 like everywhere else.

## Versions, and the publish counter

A camera that cannot change product needs nothing from this section: its own product's
versions order its images completely, and incrementing them is the whole story. That is
every ordinary fleet, and none of what follows applies to it.

Yours is the other case. A camera built with `product_id = 0` can be moved between product
lines, and two products' version numbers have nothing to say about each other — customer
A's `5.3.0` and customer B's `2.1.0` are not orderable, and a stock image at `1.0.0` is
below both. So those cameras order their images by a different number.

**`publish_seq` is the account's publish counter**: allocated by the server, strictly
increasing, never reused, and compared by nothing except anti-rollback. `platform = true`
turns it on, and from then on every `build ota-romfs` takes the next number when it
starts and stamps it into the image it signs. That has a real cost, and it is the one
thing to know before you commit: **there is no offline build with this on.** A build has
to reach the server, which means being logged in, which means your build pipeline needs a
credential. Everything else about it is free.

What the counter buys:

- **Version strings become entirely yours.** They order nothing, so a customer can retrain
  and ship `1.0.0` twice, or use dates, or whatever their UI wants. None of it can wedge a
  camera.
- **A camera can be returned to stock.** Rebuild the stock image, it takes the newest
  number, and a camera running customer A's `5.3.0` takes it — even though `1.0.0` reads
  as older, because the version is not what is being compared.
- **Deliberate rollback works.** If a model regresses, republish the previous image as a
  new release. It gets a fresh number, so the fleet takes it. Anti-rollback here means
  *no older artifact*, not *no older code*.

And the discipline that comes with it, which is not enforceable and matters:

> A higher counter means **published later**, not **contains more**. If a rebuild goes out
> from an older branch of your runtime it still gets a higher number and cameras will take
> it. Build every image from current source, or the counter will happily walk a fleet
> backwards through code while moving forwards through numbers.

The upside of one shared runtime is the other half of that: a fix in it reaches every
customer on their next build, and no camera can be walked below what it is already running.

### How the numbers are handed out

```bash
openmv-ota build ota-romfs ./projects/acme -b OPENMV_N6   # takes the next number itself
```

A database sequence hands these out at millions per second, so the round trip is the cost,
not the contention. Gaps are fine — a build that fails after taking a number simply burns
it, and nothing anywhere requires them to be contiguous.

Do not try to remove the round trip with a block allocator — a worker grabbing a thousand
numbers and handing them out locally. It breaks the one property the whole arrangement
rests on: that a freshly built image has a higher number than whatever a camera is
running. Claim and return both depend on it.

Out-of-order publishing is fine and expected at any real build rate. The server checks a
number against **that product's** newest, not the account's, so a build that finishes
second is not refused for it. Account-wide ordering is enforced at the offer instead: a
camera reports what it is on, and the server never hands it a release below that.

### The build byte

`payload_version` — your app's version, not the counter — is a uint32 packed as
`major.minor.patch.build`, one byte each, and the fourth is a **build number**:

```json
{ "app_version": "1.4.2.7" }
```

Three components give 2**24 versions per product, which is generous for a product line and
less so for something rebuilt per workflow change. The fourth byte takes the pressure off:
rebuild `1.4.2` as `1.4.2.1`, `1.4.2.2` and so on without touching the number your
customers see, and `1.4.3` still sorts above all 256 of them. Leave it off and it is zero,
which is what a three-component version has always encoded.

### Resetting a camera

Returning one to stock is a pin to the **current** stock release — a forward step under
the counter, never a downgrade, and an ordinary OTA install rather than a reflash.

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
first check-in lands where you expect. Pins work the same way, so binding and claiming can
both happen at the point of sale, and the camera arrives already knowing what it is.

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
(see [Release artifacts](08-release-artifacts.md)).

The signer is pluggable — an encrypted PEM at minimum, and PKCS#11, AWS/GCP/Azure KMS, or
your own hook ([Signing keys](05-signing-keys.md)). At platform scale, a key in a KMS is
worth the setup: you will be signing unattended, on a schedule, for a long time.

Publishing is the same verb as anywhere:

```bash
openmv-ota client release publish ./projects/acme -b OPENMV_N6
```

With a product per customer this runs once per customer per board, against the same
account and the same counter.

## Credentials

Your server holds an account token and uses it for everything. There is no impersonation —
a token's account comes from the token itself, and there is no "act as" header — so if
you are ever issued more than one account, you hold a credential for each and choose per
call.

Scopes are the ladder from [Accounts and tokens](19-accounts-and-tokens.md): `publish` >
`manage` > `observe`. Give a build pipeline `publish`, a dashboard `observe`, and keep
them separate so a leak from one is not a leak from both.

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
in [Pulling device data](24-pulling-device-data.md). Two shapes, and the difference
matters when you plan a product around it: `logs/{topic}` returns records with a
`before_seq` cursor, so it backfills completely; `series/{topic}` returns aggregated
buckets (`t`, `n`, `min`, `max`, `avg`) rather than samples. There is no raw-sample export
for numeric telemetry.

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

## What the counter does and does not protect

Worth stating plainly, because the arrangement is unusual enough that people will ask.

**It does stop an old image being served to a camera.** Every artifact carries a number,
the camera records the highest it has run, and the firmware refuses anything below it —
whatever product that artifact belongs to, and whatever its version string says. Someone
who can answer a camera's check-ins and holds a copy of an old signed release still cannot
install it. That is the whole point of the counter being global to the account rather than
per product: it spans the moves a `product_id = 0` camera can make.

**It does not stop you shipping old code.** A republished old image gets a fresh number and
cameras take it, which is the rollback feature above, seen from the other side. The
guarantee is *no older artifact*, not *no older code* — and it is why building every image
from current source is a real requirement and not a style note.

**It does not separate your customers on the device.** That is the server's targeting, as
the keys section says.

**None of it survives physical access.** The keys are in firmware that is identical across
your fleet, so a flash dump recovers them. That is true of payload encryption too, and it
stays true until readout protection and fuses land.

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

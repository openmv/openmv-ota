# Threat model

> Not a stub -- this is the threat model. [architecture.md](architecture.md) and
> [v2-plan.md](v2-plan.md) give the design it defends.

**In scope:** OTA-borne threats — signed-or-unsigned artefacts pushed over a
possibly-controlled network. Defended with ECDSA signatures (COSE algorithm ids,
P-256 by default; see [trailer.md](trailer.md)), key rotation/revocation,
anti-rollback, and a one-shot trial-boot rollback.

**Out of scope:** local USB / SWD / JTAG access, DFU reflash, hardware fault
injection, side channels, network transport attacks (app-layer TLS/cert-pinning),
and compromise of the signing infrastructure. Anyone with bus access on these
boards can do anything — that's accepted.

**Explicit non-goals:** image confidentiality (no encryption), multi-signature per
image, in-field OTA-only key revocation, persistent counters outside the partition.

**No longer non-goals.** *Resumable downloads* were listed here too, and are now built:
`_ResumingBody` restarts a dropped transfer at the compressed offset already consumed
instead of re-running the whole install. It changes nothing below — a resumed stream is
verified exactly like an uninterrupted one.

*Delta updates*, also once on this list, are built
and are the default representation on the fleet. They change nothing here: a delta is
pure transport. The reconstructed image is verified by the manifest's sha256 and then
by the trailer signature on boot, so a patch is never trusted -- a corrupted or hostile
one fails those checks exactly as a corrupted full image does.

**Key custody (operational, assumed not enforced by tooling):** private signing
keys (`keys/private/*.pem`, both `ota` and `factory` roles) never leave the party
that owns them. A contract manufacturer receives the *signed binary*
(`<board>-factory-romfs.img`) and flashes it — never a private key, the project, or this
tool. If a third party must sign on their own hardware, do it through a service or
HSM where the key never materialises on their machine, not by copying a `.pem`. The
public halves in `trusted_keys.json` are not secret and ship on every device. A
factory `key_id` is for **attribution** (which production run signed an image) and
`revoke`, not for limiting how many units a manufacturer flashes — over-production
is metered by per-device registration (unique id-bound credentials issued at flash
time), a separate mechanism from image signing.

**Account scoping + the sticky device→account binding (multi-tenancy).** A
product is namespaced by its maker's account, and the OTA server enforces that
scope on every admin path (§ [the update server](../tutorial/06-update-server.md)). The device's authoritative account
is a **sticky binding**: *learned* from the first check-in that reports a non-empty
account, then never downgraded by a later report (so a fallback boot reporting the older
image's baked account — often `''` — can't strand a device that was healthy under a
real account), or set by an *admin* override.

Trust assumptions, explicit because check-ins are **unauthenticated** (the device
holds no key, so it can't prove its account):

- The account a device *claims* is a hint that bootstraps the learned binding; it is
  **not** authenticated. The real guarantees are (a) the server never offers a device
  a release outside its bound account, and (b) even a mis-offered image **won't
  install** — the image signature is verified on-device against firmware-baked keys,
  so a cross-account image (different keys) is rejected. Worst case of a spoofed or
  griefed account is a *mis-offer* of signed bytes that can't be installed.
- A griefer who knows a registered device's id can *learn*-bind it to their own
  account first; the operator override recovers it (an admin binding overrides a
  learned one). One account **cannot** steal a device another account has
  *admin*-bound (that returns 404). Admin-bind racing between accounts is last/first-
  writer-wins; on a shared/hosted server, gate who may call the bind endpoint by
  proof of ownership (the website mediates) — there is no cryptographic ownership
  proof at this layer.
- Only a **registered** device (past the swd-ids gate) can ever create a binding, so
  the binding table is bounded by the registered fleet — a fake id can't grow it.
- `registrar_ref` (which factory/form-key registered a unit) is **per-factory and
  shared** across makers; it is never used to infer the account.

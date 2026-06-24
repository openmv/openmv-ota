# Threat model

> Stub. See [../openmv-romfs-ota-concept-plan.md](../openmv-romfs-ota-concept-plan.md)
> ("Threat model", "Out of scope", "Concept scope: explicit non-goals").

**In scope:** OTA-borne threats — signed-or-unsigned artefacts pushed over a
possibly-controlled network. Defended with ECDSA signatures (COSE algorithm ids,
P-256 by default; see [trailer.md](trailer.md)), key rotation/revocation,
anti-rollback, and a one-shot trial-boot rollback.

**Out of scope:** local USB / SWD / JTAG access, DFU reflash, hardware fault
injection, side channels, network transport attacks (app-layer TLS/cert-pinning),
and compromise of the signing infrastructure. Anyone with bus access on these
boards can do anything — that's accepted.

**Explicit non-goals:** image confidentiality (no encryption), delta updates,
multi-signature per image, in-field OTA-only key revocation, resumable downloads,
persistent counters outside the partition.

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

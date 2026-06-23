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

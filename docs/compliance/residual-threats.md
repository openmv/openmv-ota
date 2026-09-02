# Residual threats

The complement of [the CRA / RED alignment page](cra-red-alignment.md): that page
says what is shipped; this one says what is **deliberately not defended**, so an
auditor (or we, later) can read the risk acceptance rather than discover it. Same honesty rule —
a threat leaves this list only when the control that closes it actually exists.

How the shipped controls work is tutorial material: signing and custody
([keys](../tutorial/05-signing-keys.md), [factory](../tutorial/07-factory-and-firmware.md)),
boot verification and anti-rollback ([boot and rollback](../tutorial/11-boot-and-rollback.md)),
install integrity ([the device library](../tutorial/12-device-library.md)), and the
account/binding trust story ([accounts and tokens](../tutorial/19-accounts-and-tokens.md)).

| Threat | Status | The exposure, and what bounds it |
|---|---|---|
| **Image confidentiality** | **Planned** | Images are signed, not encrypted: the application is readable by anyone holding the bytes — a download capability, or the flash itself. Integrity and authenticity are unaffected. On-device image encryption is planned. |
| **Local bus access** (USB / SWD / JTAG, DFU reflash) | **Planned** | Anyone with the hardware on a bench can read flash, reflash it, or lift what `/flash` holds. Today that is accepted; device lockdown (debug-port and boot protection) is planned. |
| **Hardware fault injection / side channels** | Accepted | Out of scope for this device class. |
| **Compromise of the signing infrastructure** | Accepted (operational) | Tooling cannot defend the machine that signs. The controls are operational — encrypted-at-rest keys, external signing backends, and a manufacturer who receives only the finished signed binary — plus `keys revoke` to bound the blast radius after the fact. |
| **In-field key revocation without a firmware update** | Accepted | The trust store is baked into the firmware, so revoking a key reaches devices as a firmware update — there is no OTA-only revocation channel. |
| **Multi-signature per image** | Accepted | One key signs an image. Per-run key ids give attribution and targeted revocation instead of co-signing. |
| **Unauthenticated check-ins** | Accepted | A device holds no per-device secret, so a check-in cannot *prove* who it is. Bounds: offers never leave the device's bound account; a mis-offered image cannot install (signature vs firmware-baked keys); a griefed learned binding is recoverable by an admin bind; and only registered devices can create bindings, so the table is bounded by the registered fleet. There is no cryptographic ownership proof at this layer — on a shared server, who may bind is gated above the API. |
| **Update withholding** | Accepted | A network position can delay or block check-ins and downloads; it can never alter or inject (TLS in transit, and the image signature is the integrity boundary regardless). The device retries indefinitely, and a stale fleet is visible server-side. |
| **Forced downgrade to the previous release** | Accepted (inherent to A/B) | Forcing trials to fail returns the device to its previous *confirmed* release — never anything older, because the anti-rollback floor gates every install. mcuboot shares this property. |
| **Rollback-floor persistence outside the partition** | Accepted | The floor lives in the slot's control sectors, so a full physical reflash resets it. Reaching it requires the bus access already accepted above. |

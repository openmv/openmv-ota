# EU CRA / RED 3.3 alignment

How this stack maps onto the Cyber Resilience Act, RED Article 3.3(d)(e)(f) and
EN 18031. **Audited against the regulation's published text (EUR-Lex CELEX
32024R2847, Annex I quoted per requirement) and against the codebase on
2026-09-02** — every "shipped" claim below was verified to exist. The tables are
the **authoritative** version (the checklist template points here).

The Cyber Resilience Act ([Regulation (EU) 2024/2847](https://eur-lex.europa.eu/eli/reg/2024/2847))
is in force; full compliance deadline 11 December 2027, and the **Article 14
reporting obligations — actively exploited vulnerabilities and severe incidents
reported to ENISA within 24 hours — apply from 11 September 2026**. The Radio
Equipment Directive Article 3.3(d)(e)(f)
([Delegated Regulation 2022/30](https://eur-lex.europa.eu/eli/reg_del/2022/30))
is mandatory for radio equipment from 1 August 2025. EN 18031-1/2/3 are the
harmonised standards.

The tables say what is **shipped** and what is **planned** — an auditor reading
this must never find a claimed control that does not exist. The inverse list —
the threats this stack deliberately does **not** defend against — is
[residual-threats.md](residual-threats.md). Shipped rows describe code in this
repository, tested at enforced 100% coverage and, for the update path, exercised
on a nine-board hardware fleet including the negative cases (corrupt image, bad
signature, untrusted key, version rollback, no bootable slot).

### CRA Annex I Part I — essential cybersecurity requirements

Lettered per the regulation (Annex I Part I point (2)).

| Requirement | How this stack supports it |
|---|---|
| (1) Appropriate level of cybersecurity based on the risks | The design is built around a documented risk posture: [residual-threats.md](residual-threats.md) is the risk-acceptance record. The product-level cybersecurity risk assessment (Article 13(2)) is the customer's, built on it |
| (2)(a) No known exploitable vulnerabilities at making available | **Shipped:** `build sbom` exports the CycloneDX SBOM from the lock's exact pin-set — every submodule at its exact commit with its remote-derived purl — and CI runs osv-scanner over it |
| (2)(b) Secure by default configuration, incl. reset to original state | **Shipped where the stack decides:** signing is not optional (no plaintext-key mode; a production build refuses a dev key), anti-rollback is always on, and `flash factory` restores the original state. The final product's defaults (network config, app behaviour) are the customer's |
| (2)(c) Security updates, incl. automatic updates as default with opt-out, notification, postponement | **Shipped:** the device runtime's check-in loop fetches and installs updates automatically; an update is deferred while a trial is unconfirmed, and the app decides when the loop runs (its postponement mechanism). The support-period commitment is the customer's |
| (2)(d) Protection from unauthorised access + reporting of it | **Shipped server-side:** every admin path takes a scoped bearer token; releases and devices are account-scoped; devices pass a registration gate before anything is stored; downloads are one-time capability URLs; the audit log records admin access. Devices hold no credentials — the check-in trust model and its limits are in [residual-threats.md](residual-threats.md) |
| (2)(e) Confidentiality of stored and transmitted data | **Transit shipped:** device↔server traffic is TLS with the server certificate verified against the device's trust store. **At rest: planned** (on-device image encryption — residual-threats); the customer covers their own stored data |
| (2)(f) Integrity of stored/transmitted data, programs, config + reporting of corruptions | **Shipped:** ECDSA signature + SHA-256 + CRC32 on every image, verified at every boot; anti-rollback; the check-in reports slot state and fallback, so a corrupted or rejected image is visible fleet-wide. Customer covers /flash and /sdcard |
| (2)(g) Data minimisation | **Shipped server-side:** a check-in carries device/build state only — no personal data — and the server never stores data for an unvalidated device. App-level data handling is the customer's |
| (2)(h) Availability of essential functions after an incident, incl. DoS resilience | **Shipped:** A/B slots + trial rollback guarantee a bootable image; firmware-resident recovery when none survives; the watchdog modules keep the app supervised; the server rate-limits check-ins per IP |
| (2)(i) Minimise impact on the availability of other devices' services | The check-in cadence is server-paced (`poll_after_s`) and downloads resume at the dropped offset rather than restarting — a fleet does not hammer a shared network |
| (2)(j) Limit attack surfaces, incl. external interfaces | The device opens no listening ports — outbound HTTPS to one host is its whole network surface — and the boot trust path is deliberately parser-free. The server is one app on one port |
| (2)(k) Reduce the impact of an incident (exploitation mitigation) | A bad update is confined to the slot it was written to and falls back; a compromised update server cannot push installable code — images verify against firmware-baked keys the server never holds |
| (2)(l) Security-related information by recording and monitoring | **Server side shipped:** every admin action lands in a hash-chained, tamper-evident audit log (`prev_hash`/`entry_hash`). On-device security-event recording is the customer's app (the shipped `openmv_log` is debug logging, not an event record) |
| (2)(m) Secure and permanent removal of user data and settings | `flash erase` wipes the user disk and `flash erase --romfs` the application images; data the customer's app stores elsewhere is the customer's to remove |

### CRA Annex I Part II — vulnerability handling requirements

| Requirement | How this stack supports it |
|---|---|
| (1) Identify and document components; SBOM in a machine-readable format | **Shipped:** `build sbom` renders CycloneDX 1.5 from the lock (firmware commit, every submodule commit, toolchain versions) — deterministic per lock, exportable in CI from a bare clone |
| (2) Address and remediate vulnerabilities without delay; security updates separable from feature updates | **Shipped:** OTA delivery with staged rollouts and per-device/cohort pins; publishing a security-only release is ordinary release discipline (nothing couples an image to feature work) |
| (3) Effective and regular tests and reviews | **Shipped:** 2,000+ host tests at *enforced* 100% coverage; `boot.py` and the installer run on real MicroPython under QEMU; the on-device ECDSA shim checked against the firmware's own mbedtls; the update path exercised on a nine-board hardware fleet on every pull request, adversarial cases included |
| (4) Publicly disclose fixed vulnerabilities once an update is available | Disclosure policy template (shipped); the vendor's policy names its own advisory channel |
| (5) Coordinated vulnerability disclosure policy | `vuln-disclosure-policy` template (shipped) |
| (6) Facilitate vulnerability reporting; provide a contact address | `security.txt` template (RFC 9116) + the disclosure policy's contact and timeline |
| (7) Mechanisms to securely distribute updates | **Shipped — this is the product:** signed images verified on-device against firmware-baked keys (the server is not trusted), TLS transport, one-time capability download URLs, automatic installation via the check-in loop |
| (8) Security updates disseminated without delay, free of charge, with advisory messages | OTA delivery has no per-device fee; advisory messaging is the vendor's channel (disclosure policy template) |

### RED 3.3 (Article 3.3(d)(e)(f))

| Requirement | Coverage |
|---|---|
| 3.3(d) Network protection | Device-side TLS with a verified trust store (pinned root supported for self-hosts); no listening ports; the image signature holds even against a TLS-layer failure |
| 3.3(e) Personal data protection | Customer (app design); the check-in itself carries no personal data, and `build sbom` exports the exact dependency pin-set for data-handling audits |
| 3.3(f) Fraud prevention | Signature on every image; anti-rollback prevents downgrade attacks |

### EN 18031 test alignment

Where the harmonised standards specify test cases, this stack maps onto them:
- EN 18031-1 (general): update mechanism, integrity protection, secure storage of
  cryptographic material.
- EN 18031-2 (data confidentiality): transit is covered (TLS); at-rest image
  encryption is planned — see [residual-threats.md](residual-threats.md).
- EN 18031-3 (fraud prevention): signature verification + anti-rollback covers
  the relevant test cases.

### What the customer still owns (CRA compliance is per-product, not per-component)

This stack is a *component*. The customer placing the final product on the market
is responsible for:

- The product-level **cybersecurity risk assessment** (CRA Article 13(2)) and
  technical documentation.
- Defining and committing to the **support period** (CRA Article 13(8): at least
  5 years, unless the product is expected to be in use for less).
- The **conformity assessment** under CRA Article 32, the **EU Declaration of
  Conformity** and CE marking.
- The customer's own **vulnerability handling process** and Article 14 reporting.
- Product-specific security requirements the OTA mechanism does not decide (no
  default passwords, the app's own secure-by-default configuration, data
  handling).

The conformity assessment checklist we ship makes these explicit so nothing is
missed.

## The templates we ship

Filling these in is the customer's job — openmv-ota is a *component*, and CRA
conformity is assessed per **product**. `openmv-ota project new --ota` copies
them into every OTA project as `compliance/`, so the paperwork starts where the
product lives; the sources ship in the package
([`src/openmv_ota/project/compliance_templates/`](../../src/openmv_ota/project/compliance_templates/)):

| Template | Who issues it | Covers |
|---|---|---|
| [`conformity-assessment-checklist.md.template`](../../src/openmv_ota/project/compliance_templates/conformity-assessment-checklist.md.template) | the customer, in their technical documentation | the Annex I tables above, as a fill-in checklist with a "customer must add" column |
| [`eu-doc.md.template`](../../src/openmv_ota/project/compliance_templates/eu-doc.md.template) | the customer, placing the product on the market | the EU Declaration of Conformity behind the CE marking (CRA Art. 28 / Annex V) |
| [`vuln-disclosure-policy.md.template`](../../src/openmv_ota/project/compliance_templates/vuln-disclosure-policy.md.template) | the vendor, published | coordinated disclosure — CRA Annex I Part II (4)(5)(6) |
| [`security.txt.template`](../../src/openmv_ota/project/compliance_templates/security.txt.template) | the vendor, served at `/.well-known/security.txt` | RFC 9116 contact + a pointer to the disclosure policy |

# EU CRA / RED 3.3 alignment

How this stack maps onto the Cyber Resilience Act, RED Article 3.3(d)(e)(f) and
EN 18031. Preserved from the original concept plan when that document was retired, and
**audited against the codebase 2026-08-19** — every "shipped" claim below was
verified to exist. The tables are the **authoritative** version (the checklist
template points here).

Corrected for **v2** where the original described the retired v1 model: there is no
immutable "golden" image any more. The two slots are equal and both updatable,
ordered by an install counter, so the image behind the running one is *the last
release that worked* rather than the factory build. See
[../architecture.md](../reference/architecture.md).

The Cyber Resilience Act ([Regulation (EU) 2024/2847](https://eur-lex.europa.eu/eli/reg/2024/2847)) is in force; full compliance deadline 11 December 2027, and the **Article 14 reporting obligations — actively exploited vulnerabilities and severe incidents reported to ENISA within 24 hours — apply from 11 September 2026**. The Radio Equipment Directive Article 3.3(d)(e)(f) ([Delegated Regulation 2022/30](https://eur-lex.europa.eu/eli/reg_del/2022/30)) is mandatory for radio equipment from 1 August 2025. EN 18031-1/2/3 are the harmonised standards.

The tables say what is **shipped** and what is **planned** — an auditor reading this
must never find a claimed control that does not exist. Shipped rows describe code in
this repository, tested at enforced 100% coverage and, for the update path, exercised
on a nine-board hardware fleet including the negative cases (corrupt image, bad
signature, untrusted key, version rollback, no bootable slot).

### CRA Annex I — essential cybersecurity requirements

| Requirement | How this stack supports it |
|---|---|
| 1(2)(a) Free of known exploitable vulnerabilities at placing on market | **Shipped:** `build sbom` exports the CycloneDX SBOM from the lock's exact pin-set — including every submodule at its exact commit with its remote-derived purl — and CI runs osv-scanner over it |
| 1(2)(b) Secure by default configuration | Documented in the conformity assessment template; customer applies |
| 1(2)(c) Security updates throughout support period | OTA mechanism in this plan; vendor commits to a support period |
| 1(2)(d) Protection against unauthorised access | ECDSA (P-256) signatures + anti-rollback + fallback to the other slot |
| 1(2)(e) Confidentiality of stored data | **Out of scope** — customer must add encryption if applicable. Documented as explicit non-goal. |
| 1(2)(f) Integrity of stored data | Signature + SHA-256 + CRC32 for ROMFS; customer covers /flash and /sdcard |
| 1(2)(g) Data minimisation | Customer (app design); we provide guidance |
| 1(2)(h) Availability of essential functions | Trial-boot + A/B slots guarantee one bootable image always exists |
| 1(2)(i) Minimise attack surface | Customer (app design); we provide guidance |
| 1(2)(j) Mitigate impact of incidents | Automatic fallback to the other slot on a bad update |
| 1(2)(k) Security event recording | **Server side shipped:** every admin action lands in a hash-chained, tamper-evident audit log (`prev_hash`/`entry_hash`). On-device security-event recording is the customer's app (the shipped `openmv_log` is debug logging, not an event record) |
| 1(2)(l) Secure deletion of data | Customer (app design) |
| 1(2)(m) Vulnerability handling throughout support period | **Shipped:** OTA delivery + SBOM export (`build sbom`) + the disclosure/`security.txt` templates |

### CRA Annex I — vulnerability handling requirements

| Requirement | How this stack supports it |
|---|---|
| 2(1) Identify components in product → SBOM | **Shipped:** `build sbom` renders CycloneDX 1.5 from the lock (firmware commit, every submodule commit, toolchain versions) — deterministic per lock, exportable in CI from a bare clone |
| 2(2) Address vulnerabilities promptly | **Shipped:** OTA delivery with staged rollouts and per-device/cohort pins. (A public transparency log is not built) |
| 2(3) Effective testing | **Shipped:** ~1,950 host tests at *enforced* 100% coverage; `boot.py` slot logic run on real MicroPython under QEMU; the on-device ECDSA shim checked against the firmware's own mbedtls; and the update path exercised on a nine-board hardware fleet including the adversarial cases (corrupt image, tampered manifest, untrusted key, rollback, no bootable slot) |
| 2(4) Public disclosure of fixed vulnerabilities | Disclosure policy template (shipped). A fixed advisory format is not defined — the vendor's policy names its own channel |
| 2(5) Coordinated disclosure policy | `security.txt` template |
| 2(6) Mechanism to share vulnerability info | Disclosure policy template defines contact and timeline |
| 2(7) Provide updates without delay, free of charge | OTA delivery, no per-device fee |

### RED 3.3 (Article 3.3(d)(e)(f))

| Requirement | Coverage |
|---|---|
| 3.3(d) Network protection | TLS + cert pinning (app); signatures defend against TLS-layer failure |
| 3.3(e) Personal data protection | Customer (app design); `build sbom` exports the exact dependency pin-set for data-handling audits |
| 3.3(f) Fraud prevention | Signature on every image; anti-rollback prevents downgrade attacks |

### EN 18031 test alignment

Where the harmonised standards specify test cases, this stack maps onto them:
- EN 18031-1 (general): update mechanism (Section 6.x), integrity protection, secure storage of cryptographic material
- EN 18031-2 (data confidentiality): out of scope by design — customer adds encryption
- EN 18031-3 (fraud prevention): signature verification + anti-rollback covers the relevant test cases

### What the customer still owns (CRA compliance is per-product, not per-component)

This stack is a *component*. The customer placing the final product on the market is responsible for:
- Defining and committing to the **support period** (CRA Article 13(2): no shorter than expected product lifetime, minimum 5 years for many categories).
- Producing the **conformity assessment** under CRA Article 32.
- The customer's own **vulnerability handling process** (CRA Article 13).
- Customer-specific security requirements not covered by the OTA mechanism (no default passwords, customer's own network code's secure-by-default config, etc.).
- **EU Declaration of Conformity** and CE marking.

The conformity assessment checklist we ship makes these explicit so nothing is missed.

## The templates we ship

Filling these in is the customer's job — openmv-ota is a *component*, and CRA conformity is
assessed per **product**. They live in [`compliance-templates/`](../../compliance-templates/):

| Template | Who issues it | Covers |
|---|---|---|
| [`conformity-assessment-checklist.md.template`](../../compliance-templates/conformity-assessment-checklist.md.template) | the customer, in their technical documentation | the Annex I table above, as a fill-in checklist with a "customer must add" column |
| [`eu-doc.md.template`](../../compliance-templates/eu-doc.md.template) | the customer, placing the product on the market | the EU Declaration of Conformity behind the CE marking (CRA Art. 32) |
| [`vuln-disclosure-policy.md.template`](../../compliance-templates/vuln-disclosure-policy.md.template) | the vendor, published | coordinated disclosure — CRA Annex I 2(4)(5)(6) |
| [`security.txt.template`](../../compliance-templates/security.txt.template) | the vendor, served at `/.well-known/security.txt` | RFC 9116 contact + a pointer to the disclosure policy |

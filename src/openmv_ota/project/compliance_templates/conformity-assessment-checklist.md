# CRA Conformity Assessment Checklist — {{PRODUCT_NAME}}

> Maps the openmv-ota stack onto each CRA Annex I requirement, showing what this
> stack provides and what {{VENDOR_NAME}} must add. The customer includes a
> completed copy in their technical documentation when self-certifying.
>
> The authoritative mapping table lives in
> [the CRA / RED alignment page](https://github.com/openmv/openmv-ota/blob/main/docs/compliance/cra-red-alignment.md)
> (lettered per Annex I of Regulation (EU) 2024/2847); this template is the
> customer-facing fill-in version. Every requirement is listed — a row with "—"
> in the customer column still needs the checkbox: confirm it holds for the
> final product.

## Annex I Part I — essential cybersecurity requirements

| Req | Provided by openmv-ota | Customer must add | Status |
|---|---|---|---|
| (1) Appropriate cybersecurity based on the risks | The documented risk posture ([the residual-threats register](https://github.com/openmv/openmv-ota/blob/main/docs/compliance/residual-threats.md)) | Product-level risk assessment (Art. 13(2)) | ☐ |
| (2)(a) No known exploitable vulns at making available | `build sbom` CycloneDX export + osv-scanner in CI (firmware, submodules, tools at exact pins) | Track and act on findings | ☐ |
| (2)(b) Secure by default, incl. reset to original state | Signing always on (no plaintext-key mode), anti-rollback always on, `flash factory` restores original state | The product's own defaults (network, app, no default passwords) | ☐ |
| (2)(c) Security updates, automatic by default, opt-out, postponement | Check-in loop installs updates automatically; app controls when it runs | Commit to a support period (Art. 13(8)); document update UX | ☐ |
| (2)(d) Protection from unauthorised access + reporting | Scoped admin tokens, account scoping, registration gate, capability URLs, audit log | Access control in the product's own interfaces | ☐ |
| (2)(e) Confidentiality of stored and transmitted data | Transit: TLS with verified trust store. At rest: image encryption is planned, not shipped | Encrypt at-rest data the app stores, if applicable | ☐ |
| (2)(f) Integrity of data, programs, config + corruption reporting | Signature + SHA-256 + CRC32 verified every boot; check-in reports slot state/fallback | Cover /flash, /sdcard | ☐ |
| (2)(g) Data minimisation | Check-in carries device/build state only; nothing stored for unvalidated devices | The app's own data handling | ☐ |
| (2)(h) Availability of essential functions, DoS resilience | A/B + trial rollback, firmware-resident recovery, watchdog modules, server rate limiting | — | ☐ |
| (2)(i) Minimise impact on other devices' services | Server-paced check-in cadence; resumable downloads | The app's own network behaviour | ☐ |
| (2)(j) Limit attack surfaces | Device: no listening ports, outbound HTTPS to one host, parser-free boot path | The product's other interfaces | ☐ |
| (2)(k) Reduce impact of an incident | Bad update confined to one slot; server can't push installable code (keys are firmware-baked) | — | ☐ |
| (2)(l) Security-relevant recording and monitoring | Hash-chained server audit log (all admin actions) | Device-side event recording in the app | ☐ |
| (2)(m) Secure permanent removal of data and settings | `flash erase` (user disk), `flash erase --romfs` (application images) | Removal of data the app stores elsewhere | ☐ |

## Annex I Part II — vulnerability handling

| Req | Provided | Customer must add | Status |
|---|---|---|---|
| (1) SBOM | `build sbom` — CycloneDX 1.5 rendered from the lock, deterministic per build | Attach to technical documentation | ☐ |
| (2) Remediate without delay; security updates separable | Daily + on-publish OSV scans of every in-rotation release's SBOM (`client advisories`), audited findings history; OTA staged rollouts + pins for the fix; security-only releases are ordinary releases | Review findings; operate the release process | ☐ |
| (3) Effective and regular testing | 2,000+ host tests at enforced 100% coverage + QEMU boot runs + per-PR hardware-fleet adversarial catalog | App-level tests | ☐ |
| (4) Publicly disclose fixed vulnerabilities | Disclosure policy template | Publish advisories on your channel | ☐ |
| (5) Coordinated disclosure policy | `vuln-disclosure-policy` template | Publish + staff it | ☐ |
| (6) Vulnerability reporting contact | `security.txt` template (RFC 9116) | Serve it at `/.well-known/security.txt` | ☐ |
| (7) Secure update distribution | Signed images verified on-device, TLS transport, capability URLs, automatic install | Operate (or subscribe to) the update server | ☐ |
| (8) Updates without delay, free of charge, with advisories | OTA delivery, no per-device fee | Advisory messaging | ☐ |

## RED 3.3 (Article 3.3(d)(e)(f))

| Req | Coverage | Status |
|---|---|---|
| 3.3(d) Network protection | Device-side TLS with verified trust store; no listening ports; signatures hold even against TLS failure | ☐ |
| 3.3(e) Personal data protection | Check-in carries no personal data; app data handling is the customer's | ☐ |
| 3.3(f) Fraud prevention | Per-image signature + anti-rollback | ☐ |

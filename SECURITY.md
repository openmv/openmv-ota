# Security policy

openmv-ota is security tooling — a vulnerability in it can affect every fleet
built on it, so reports are taken seriously and handled ahead of feature work.

## Reporting a vulnerability

Please report suspected vulnerabilities **privately** via GitHub's security
advisories: [Report a vulnerability](https://github.com/openmv/openmv-ota/security/advisories/new).
Please do not open a public issue for a suspected vulnerability.

You can expect an acknowledgement within 7 days and coordinated disclosure once
a fix ships — fixed vulnerabilities are disclosed in the repository's security
advisories.

## Supported versions

Pre-release: `main` is the only supported line. Once released, this section
will name the supported release lines.

## Scope notes for researchers

- What the stack deliberately does **not** defend against is documented in
  [the residual-threats register](docs/compliance/residual-threats.md) — a
  report that one of those holds is expected behaviour, not a vulnerability.
- The trust boundary is the **image signature verified on-device against
  firmware-baked keys**; the update server is not trusted. Findings that break
  that boundary (signature bypass, anti-rollback bypass, installing unverified
  bytes) are the highest-value reports.

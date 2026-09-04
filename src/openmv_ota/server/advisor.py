"""CVE monitoring: scan stored SBOMs against a vulnerability database.

The scanner walks every release an account's fleet still runs (or an active
rollout still offers), queries each SBOM component against OSV.dev, and
reconciles the findings into the ``advisories`` table -- new findings appear,
repeats refresh, and findings a later scan no longer reports are cleared (the
row history is the CRA-facing evidence that monitoring ran). Every scan is
audited.

``OsvClient`` is injectable (``create_app(osv=...)``): tests and the sim
substitute a fake; a deployment talks to the real https://api.osv.dev.
Coverage caveat: OSV matches ecosystem packages well and C firmware libraries
by name/version only, so it is one source, not the last word -- the interface
takes components and returns findings, so a second source can slot in later.
"""

from __future__ import annotations

import json

_OSV_URL = "https://api.osv.dev"
_BATCH = 500                                   # OSV caps querybatch at 1000


# CVSS 3.x base-score metric weights (first.org spec, section 7.4).
_CVSS3 = {
    "AV": {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2},
    "AC": {"L": 0.77, "H": 0.44},
    "UI": {"N": 0.85, "R": 0.62},
    "CIA": {"H": 0.56, "L": 0.22, "N": 0.0},
    "PR_U": {"N": 0.85, "L": 0.62, "H": 0.27},   # scope unchanged
    "PR_C": {"N": 0.85, "L": 0.68, "H": 0.5},    # scope changed
}


def _cvss3_score(vector: str) -> float | None:
    """The CVSS 3.x base score from a vector string -- the real spec math,
    because shortcuts get it exactly backwards (AC:H means HARDER to exploit,
    i.e. LESS severe, yet naively reads as 'high')."""
    import math

    m = dict(part.split(":", 1) for part in vector.split("/")[1:] if ":" in part)
    try:
        scope_changed = m["S"] == "C"
        iss = 1 - ((1 - _CVSS3["CIA"][m["C"]]) * (1 - _CVSS3["CIA"][m["I"]])
                   * (1 - _CVSS3["CIA"][m["A"]]))
        impact = (7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15 if scope_changed
                  else 6.42 * iss)
        pr = (_CVSS3["PR_C"] if scope_changed else _CVSS3["PR_U"])[m["PR"]]
        expl = 8.22 * _CVSS3["AV"][m["AV"]] * _CVSS3["AC"][m["AC"]] * pr * _CVSS3["UI"][m["UI"]]
    except KeyError:
        return None
    if impact <= 0:
        return 0.0
    raw = min((1.08 if scope_changed else 1.0) * (impact + expl), 10)
    return math.ceil(raw * 10) / 10                      # spec: round UP to 0.1


def _severity(vuln: dict) -> str:
    """OSV entries carry severity in one of two places: GitHub-imported
    advisories set database_specific.severity (LOW/MODERATE/HIGH/CRITICAL),
    native OSV records a CVSS vector. Map both to low/medium/high/critical;
    an entry saying neither (common for OSS-Fuzz findings) is 'unknown'."""
    ds = (vuln.get("database_specific") or {}).get("severity", "").lower()
    if ds == "moderate":
        return "medium"
    if ds in ("low", "medium", "high", "critical"):
        return ds
    for sev in vuln.get("severity") or []:
        score = str(sev.get("score", ""))
        if score.startswith("CVSS:3"):
            base = _cvss3_score(score)
            if base is None:
                continue
            if base >= 9.0:
                return "critical"
            if base >= 7.0:
                return "high"
            if base >= 4.0:
                return "medium"
            return "low" if base > 0 else "unknown"
    return "unknown"


class OsvClient:
    """The real OSV.dev client. One instance per app; detail lookups cached."""

    def __init__(self, http=None, url: str = _OSV_URL):
        if http is None:                       # pragma: no cover - wired in prod only
            import httpx
            http = httpx.Client(timeout=30)
        self._http = http
        self._url = url
        self._details: dict[str, dict] = {}

    def scan(self, components: list[dict]) -> list[dict]:
        """``components`` are CycloneDX component dicts; returns findings as
        ``{component, version, vuln_id, severity, summary}`` rows."""
        comps = [c for c in components if c.get("name") and c.get("version")]
        findings: list[dict] = []
        for i in range(0, len(comps), _BATCH):
            window = comps[i:i + _BATCH]
            queries = [{"package": {"name": c["name"]}, "version": c["version"]}
                       for c in window]
            r = self._http.post(self._url + "/v1/querybatch",
                                json={"queries": queries})
            r.raise_for_status()
            for comp, res in zip(window, r.json().get("results", [])):
                for hit in res.get("vulns") or []:
                    vuln = self._detail(hit["id"])
                    findings.append({
                        "component": comp["name"], "version": comp["version"],
                        "vuln_id": vuln.get("id", hit["id"]),
                        "severity": _severity(vuln),
                        "summary": vuln.get("summary", "")})
        return findings

    def _detail(self, vuln_id: str) -> dict:
        if vuln_id not in self._details:
            r = self._http.get(f"{self._url}/v1/vulns/{vuln_id}")
            self._details[vuln_id] = r.json() if r.status_code == 200 else {"id": vuln_id}
        return self._details[vuln_id]


def scan_release(state, rel: dict, actor: str = "scheduler") -> dict:
    """Scan ONE release's SBOM; reconcile and audit. A release without an SBOM
    has nothing to scan and reports zero findings (never an error -- evidence
    is worth carrying, not worth failing a scan run over)."""
    from .errors import ServerError

    release_id, account_id = rel["release_id"], rel.get("account_id", "")
    components: list[dict] = []
    if rel.get("sbom_key"):
        try:
            components = json.loads(state.storage.get(rel["sbom_key"])).get(
                "components", []) or []
        except (ServerError, ValueError):
            components = []
    findings = state.osv.scan(components)
    result = state.metastore.upsert_advisories(release_id, findings,
                                               account_id=account_id)
    state.metastore.append_audit(
        actor=actor, action="advisory.scan", entity_type="release",
        entity_id=release_id,
        data={"components": len(components), "findings": len(findings),
              "new": len(result["new"]), "cleared": result["cleared"]},
        account_id=account_id)
    return {"release_id": release_id, "findings": len(findings),
            "new": result["new"], "cleared": result["cleared"]}


def scan_account(state, account_id: str, actor: str = "scheduler") -> dict:
    """Scan every release the account's fleet still cares about."""
    rels = state.metastore.releases_with_devices(account_id)
    new: list[dict] = []
    findings = 0
    for rel in rels:
        out = scan_release(state, rel, actor=actor)
        findings += out["findings"]
        new.extend(out["new"])
    return {"releases_scanned": len(rels), "findings": findings, "new": new}

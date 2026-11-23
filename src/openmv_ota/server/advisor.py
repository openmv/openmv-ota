"""CVE monitoring: scan stored SBOMs against a vulnerability database.

The scanner walks every release an account's fleet still runs, queries each SBOM component against OSV.dev, and
reconciles the findings into the ``advisories`` table -- new findings appear,
repeats refresh, and findings a later scan no longer reports are cleared (the
row history is the CRA-facing evidence that monitoring ran). Every scan is
audited (``advisory.scan``), and so is every transition: ``advisory.found`` when a
finding turns active, ``advisory.cleared`` when it stops being -- each exactly once,
however scans overlap.

``OsvClient`` is injectable (``create_app(osv=...)``): tests and the sim
substitute a fake; a deployment talks to the real https://api.osv.dev.
How a component is looked up (never by bare name + version: OSV then matches every
distro's package of that name -- a "micropython 1.29.0" query answered with Debian and
Ubuntu records for unrelated versions):

- its exact git commit (the SBOM's ``openmv-ota:commit``), matched against the advisories'
  GIT ranges -- precise for any repo OSV indexes;
- for a component OSV knows by its upstream repo, the release tag of its version
  (:data:`_UPSTREAM_GIT`): OpenMV builds MicroPython from a fork whose commits OSV never
  saw, but the fork is upstream ``v<version>`` plus patches;
- its package URL (``purl``), for components from a language ecosystem (PyPI, ...).

A component with none of these is not looked up. OSV is one source, not the last word --
the interface takes components and returns findings, so a second source can slot in.
"""

from __future__ import annotations

import json
import re
import threading

_OSV_URL = "https://api.osv.dev"
_BATCH = 500                                   # OSV caps querybatch at 1000
# components whose advisories OSV keys to an upstream repo's release tags
_SHA = re.compile(r"[0-9a-f]{40}")
_UPSTREAM_GIT = {"micropython": "https://github.com/micropython/micropython"}
# The actor of a scan nobody asked for: the server's own periodic pass. An API scan is
# audited as its caller.
SCHEDULER = "scheduler"

# One scan per account at a time, in this process. The scheduler's pass runs in a worker
# thread while an API scan can arrive on a request; run side by side they would both
# reconcile the same releases. The reconcile is itself race-safe (each transition is
# reported once even across server instances); this keeps the two scans' audit trails
# from interleaving and the second from re-querying OSV for nothing new.
_scan_locks: dict[str, threading.Lock] = {}
_scan_locks_guard = threading.Lock()


MAX_DETAILS = 4096              # advisories remembered between scans, then read afresh
MAX_COMPONENTS = 5000           # a firmware's SBOM has tens; each one is queries to OSV


def _account_lock(account_id: str) -> threading.Lock:
    with _scan_locks_guard:
        return _scan_locks.setdefault(account_id, threading.Lock())


def _queries(comp: dict) -> list[dict]:
    """The OSV queries that identify one SBOM component (see the module docstring)."""
    out = []
    commit = next((p.get("value") for p in comp.get("properties") or []
                   if p.get("name") == "openmv-ota:commit" and p.get("value")), None)
    # only a full sha: OSV refuses anything else -- and one refused query fails the whole
    # batch, which would leave every component unscanned
    if commit and _SHA.fullmatch(commit):
        out.append({"commit": commit})
    repo, version = _UPSTREAM_GIT.get(comp.get("name", "")), comp.get("version")
    if repo and version:
        out.append({"package": {"name": repo, "ecosystem": "GIT"}, "version": "v" + version})
    if comp.get("purl"):
        out.append({"package": {"purl": comp["purl"]}})
    return out


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
        pairs = [(c, q) for c in components if c.get("name") for q in _queries(c)]
        findings: list[dict] = []
        seen: set = set()                    # one finding per (component, vuln), however many
        for i in range(0, len(pairs), _BATCH):   # of its queries report it
            window = pairs[i:i + _BATCH]
            r = self._http.post(self._url + "/v1/querybatch",
                                json={"queries": [q for _c, q in window]})
            r.raise_for_status()
            results = r.json().get("results", [])
            if len(results) != len(window):
                # one result per query, in order; a short or padded reply would pin vulns on
                # the wrong component, or report none -- which the reconcile then reads as
                # "fixed" and clears standing advisories. Fail the scan instead.
                raise ValueError("OSV answered %d results for %d queries"
                                 % (len(results), len(window)))
            for (comp, _q), res in zip(window, results, strict=True):
                for hit in res.get("vulns") or []:
                    if (comp["name"], hit["id"]) in seen:
                        continue
                    seen.add((comp["name"], hit["id"]))
                    vuln = self._detail(hit["id"])
                    findings.append({
                        "component": comp["name"], "version": comp.get("version") or "",
                        "vuln_id": vuln.get("id", hit["id"]),
                        "severity": _severity(vuln),
                        "summary": vuln.get("summary", "")})
        return findings

    def _detail(self, vuln_id: str) -> dict:
        got = self._details.get(vuln_id)     # (read once: another account's scan may clear it)
        if got is None:
            r = self._http.get(f"{self._url}/v1/vulns/{vuln_id}")
            if r.status_code != 200:
                # not remembered: one bad answer stood for this advisory until the server
                # restarted, as an advisory of unknown severity with no summary
                return {"id": vuln_id}
            if len(self._details) >= MAX_DETAILS:
                self._details.clear()
            got = self._details[vuln_id] = r.json()
        return got


class ScanBusy(Exception):
    """The account's scan is already running (raised only to a caller that will not wait)."""


def _locked(account_id: str, wait: bool, fn):
    """Run ``fn`` under the account's scan lock. ``wait=False`` is the API's: a request that
    queued behind a running scan held one of the server's worker threads for as long as the
    scan took, and a few dozen of them were every thread there is."""
    lock = _account_lock(account_id)
    if not lock.acquire(blocking=wait):
        raise ScanBusy
    try:
        return fn()
    finally:
        lock.release()


def scan_release(state, rel: dict, actor: str = SCHEDULER, wait: bool = True) -> dict:
    """Scan ONE release's SBOM; reconcile and audit. A release without an SBOM
    has nothing to scan and reports zero findings (never an error -- evidence
    is worth carrying, not worth failing a scan run over)."""
    return _locked(rel.get("account_id", ""), wait, lambda: _scan_release(state, rel, actor))


def _audit_cleared(state, rel_id: str, cleared: list[dict], reason: str, actor: str,
                   account_id: str, product_id) -> None:
    """One ``advisory.cleared`` per finding that stopped being active: ``not_reported`` (a scan
    of the release no longer reports it) or ``out_of_rotation`` (no device runs the
    release any more)."""
    for f in cleared:
        state.metastore.append_audit(
            actor=actor, action="advisory.cleared", entity_type="release", entity_id=rel_id,
            data={"vuln_id": f["vuln_id"], "component": f["component"],
                  "version": f.get("version", ""), "reason": reason},
            account_id=account_id, product_id=product_id)


def _scan_release(state, rel: dict, actor: str) -> dict:
    from .errors import ServerError

    release_id, account_id = rel["release_id"], rel.get("account_id", "")
    components: list[dict] = []
    if rel.get("sbom_key"):
        try:
            doc = json.loads(state.storage.get(rel["sbom_key"]))
            components = doc.get("components", []) or [] if isinstance(doc, dict) else None
            # An SBOM is the publisher's own file, checked at upload only for being JSON. One
            # that is not a table of components, or has more of them than any firmware has
            # (each is a round trip to the advisory service), is one this scan cannot read.
            if (not isinstance(components, list) or len(components) > MAX_COMPONENTS
                    or not all(isinstance(c, dict) and isinstance(c.get("properties") or [], list)
                               and all(isinstance(c.get(k) or "", str)
                                       for k in ("name", "version", "purl"))
                               and all(isinstance(p, dict)
                                       and isinstance(p.get("name") or "", str)
                                       and isinstance(p.get("value") or "", str)
                                       for p in c.get("properties") or [])
                               for c in components)):
                raise ValueError("not a component list this scan reads")
        except (ServerError, ValueError):
            # an SBOM we cannot read is not evidence the release is clean: scanning nothing
            # would reconcile to zero findings and clear every standing advisory. Keep them.
            state.metastore.append_audit(
                actor=actor, action="advisory.scan", entity_type="release",
                entity_id=release_id, data={"sbom_unreadable": True},
                account_id=account_id, product_id=rel.get("product_id"))
            return {"release_id": release_id, "findings": 0, "new": [], "cleared": 0}
    findings = state.osv.scan(components)
    result = state.metastore.upsert_advisories(release_id, findings,
                                               account_id=account_id)
    state.metastore.append_audit(
        actor=actor, action="advisory.scan", entity_type="release",
        entity_id=release_id,
        data={"components": len(components), "findings": len(findings),
              "new": len(result["new"]), "cleared": result["cleared"]},
        account_id=account_id, product_id=rel.get("product_id"))
    for f in result["new"]:                        # one event per NEW finding, not per scan
        state.metastore.append_audit(
            actor=actor, action="advisory.found", entity_type="release", entity_id=release_id,
            data={"vuln_id": f.get("vuln_id"), "component": f.get("component"),
                  "version": f.get("version", ""), "severity": f.get("severity", "unknown")},
            account_id=account_id, product_id=rel.get("product_id"))
    _audit_cleared(state, release_id, result["cleared_findings"], "not_reported", actor,
                   account_id, rel.get("product_id"))
    return {"release_id": release_id, "findings": len(findings),
            "new": result["new"], "cleared": result["cleared"]}


def scan_account(state, account_id: str, actor: str = SCHEDULER, wait: bool = True) -> dict:
    """Scan every release the account's fleet still cares about -- and CLEAR
    the findings of releases that left rotation. A release nobody runs and
    nobody offers is out of scope, and stale advisories lingering on it would
    make the security list grow forever."""
    return _locked(account_id, wait, lambda: _scan_account(state, account_id, actor))


def _scan_account(state, account_id: str, actor: str) -> dict:
    ms = state.metastore
    rels = ms.releases_with_devices(account_id)
    new: list[dict] = []
    findings = 0
    for rel in rels:
        out = _scan_release(state, rel, actor)
        findings += out["findings"]
        new.extend(out["new"])
    in_scope = {r["release_id"] for r in rels}
    for rid in ms.releases_with_active_advisories(account_id):
        if rid in in_scope:
            continue
        result = ms.upsert_advisories(rid, [], account_id=account_id)
        product_id = (ms.get_release(rid) or {}).get("product_id")
        ms.append_audit(actor=actor, action="advisory.scan", entity_type="release",
                        entity_id=rid,
                        data={"out_of_rotation": True, "cleared": result["cleared"]},
                        account_id=account_id, product_id=product_id)
        _audit_cleared(state, rid, result["cleared_findings"], "out_of_rotation", actor,
                       account_id, product_id)
    return {"releases_scanned": len(rels), "findings": findings, "new": new}

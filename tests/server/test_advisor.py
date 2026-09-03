"""The OSV client and scan plumbing -- the real logic the autouse test guard
no-ops everywhere else. A stub http object stands in for api.osv.dev."""

from __future__ import annotations

import json
from types import SimpleNamespace

from openmv_ota.server import advisor
from openmv_ota.server.advisor import OsvClient, _severity
from openmv_ota.server.metastore import SqliteMetadataStore
from openmv_ota.server.storage import LocalArtifactStorage


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        assert self.status_code == 200


class _StubHttp:
    """querybatch returns ids; per-vuln detail served from ``vulns``."""

    def __init__(self, hits, vulns):
        self.hits = hits            # {(name, version): [vuln ids]}
        self.vulns = vulns          # {id: osv json}
        self.posts = 0
        self.gets = []

    def post(self, url, json=None):
        assert url.endswith("/v1/querybatch")
        self.posts += 1
        results = []
        for q in json["queries"]:
            ids = self.hits.get((q["package"]["name"], q["version"]), [])
            results.append({"vulns": [{"id": i, "modified": "x"} for i in ids]} if ids else {})
        return _Resp({"results": results})

    def get(self, url):
        vid = url.rsplit("/", 1)[1]
        self.gets.append(vid)
        v = self.vulns.get(vid)
        return _Resp(v, 200) if v else _Resp({}, 404)


def test_osv_scan_maps_and_caches_details():
    http = _StubHttp(
        hits={("mbedtls", "3.5.1"): ["CVE-1", "CVE-2"], ("lwip", "2.1.3"): ["CVE-1"]},
        vulns={"CVE-1": {"id": "CVE-1", "summary": "overflow",
                         "database_specific": {"severity": "HIGH"}},
               "CVE-2": {"id": "CVE-2", "summary": "dos",
                         "database_specific": {"severity": "MODERATE"}}})
    c = OsvClient(http=http, url="https://osv.test")
    comps = [{"name": "mbedtls", "version": "3.5.1"},
             {"name": "lwip", "version": "2.1.3"},
             {"name": "nameless"},                      # skipped: no version
             {"version": "1.0"}]                        # skipped: no name
    out = advisor.REAL_OSV_SCAN(c, comps)
    assert [(f["component"], f["vuln_id"], f["severity"]) for f in out] == [
        ("mbedtls", "CVE-1", "high"), ("mbedtls", "CVE-2", "medium"),
        ("lwip", "CVE-1", "high")]
    assert out[0]["summary"] == "overflow"
    assert http.gets.count("CVE-1") == 1                # detail cached across hits


def test_osv_detail_404_falls_back_to_id():
    http = _StubHttp(hits={("x", "1"): ["GHSA-xyz"]}, vulns={})
    out = advisor.REAL_OSV_SCAN(OsvClient(http=http, url="u"), [{"name": "x", "version": "1"}])
    assert out == [{"component": "x", "version": "1", "vuln_id": "GHSA-xyz",
                    "severity": "unknown", "summary": ""}]


def test_severity_mapping():
    assert _severity({"database_specific": {"severity": "CRITICAL"}}) == "critical"
    assert _severity({"database_specific": {"severity": "MODERATE"}}) == "medium"
    assert _severity({"severity": [{"type": "CVSS_V3",
                                    "score": "CVSS:3.1/AV:N/C:H/I:N"}]}) == "high"
    assert _severity({"severity": [{"type": "CVSS_V3",
                                    "score": "CVSS:3.1/AV:N/C:L/I:N"}]}) == "medium"
    assert _severity({"severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N"}]}) == "unknown"
    assert _severity({"severity": [{"score": "not-a-vector"}]}) == "unknown"
    assert _severity({}) == "unknown"


def _state(tmp_path):
    ms = SqliteMetadataStore(str(tmp_path / "a.db"))
    ms.migrate()
    return SimpleNamespace(metastore=ms, storage=LocalArtifactStorage(str(tmp_path / "b")),
                           osv=SimpleNamespace(scan=lambda comps: []))


def test_scan_release_without_or_with_bad_sbom(tmp_path):
    st = _state(tmp_path)
    # no sbom at all: zero findings, still audited
    out = advisor.scan_release(st, {"release_id": "r1", "account_id": "a"})
    assert out["findings"] == 0
    # sbom key present but the bytes are gone (retention) -> still not an error
    out = advisor.scan_release(st, {"release_id": "r2", "account_id": "a",
                                    "sbom_key": "sbom/gone.json"})
    assert out["findings"] == 0
    # sbom present but not JSON -> ditto
    st.storage.put("sbom/bad.json", b"not json", "application/json")
    out = advisor.scan_release(st, {"release_id": "r3", "account_id": "a",
                                    "sbom_key": "sbom/bad.json"})
    assert out["findings"] == 0
    assert len([e for e in st.metastore.read_audit()
                if e["action"] == "advisory.scan"]) == 3


def test_scan_release_records_findings(tmp_path):
    st = _state(tmp_path)
    st.storage.put("sbom/ok.json", json.dumps(
        {"components": [{"name": "mbedtls", "version": "3.5.1"}]}).encode(),
        "application/json")
    st.osv = SimpleNamespace(scan=lambda comps: [
        {"component": "mbedtls", "version": "3.5.1", "vuln_id": "CVE-9",
         "severity": "high", "summary": "s"}])
    out = advisor.scan_release(st, {"release_id": "r1", "account_id": "a",
                                    "sbom_key": "sbom/ok.json"})
    assert out["findings"] == 1 and out["new"][0]["vuln_id"] == "CVE-9"
    rows = st.metastore.list_advisories(account_id="a")
    assert rows[0]["release_id"] == "r1" and rows[0]["severity"] == "high"


def test_scheduler_disabled_at_zero_interval():
    from openmv_ota.server.cli import _schedule_advisory_scans

    class _App:                                  # would explode if a handler registered
        def on_event(self, *_):
            raise AssertionError("must not arm the loop")
    _schedule_advisory_scans(_App(), SimpleNamespace(advisory_scan_interval_s=0))

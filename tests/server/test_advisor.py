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
    """querybatch returns ids; per-vuln detail served from ``vulns``. A query is keyed by
    what identifies it: ("commit", sha), ("git", repo, tag) or ("purl", purl)."""

    def __init__(self, hits, vulns):
        self.hits = hits            # {query key: [vuln ids]}
        self.queries = []
        self.vulns = vulns          # {id: osv json}
        self.posts = 0
        self.gets = []

    def post(self, url, json=None):
        assert url.endswith("/v1/querybatch")
        self.posts += 1
        results = []
        for q in json["queries"]:
            self.queries.append(q)
            pkg = q.get("package") or {}
            key = (("commit", q["commit"]) if "commit" in q else
                   ("purl", pkg["purl"]) if "purl" in pkg else
                   ("git", pkg["name"], q["version"]))
            ids = self.hits.get(key, [])
            results.append({"vulns": [{"id": i, "modified": "x"} for i in ids]} if ids else {})
        return _Resp({"results": results})

    def get(self, url):
        vid = url.rsplit("/", 1)[1]
        self.gets.append(vid)
        v = self.vulns.get(vid)
        return _Resp(v, 200) if v else _Resp({}, 404)


def _commit(sha):
    return [{"name": "openmv-ota:commit", "value": sha}]


def test_osv_scan_maps_and_caches_details():
    http = _StubHttp(
        hits={("commit", "a" * 40): ["CVE-1", "CVE-2"], ("purl", "pkg:pypi/tool@2.1"): ["CVE-1"]},
        vulns={"CVE-1": {"id": "CVE-1", "summary": "overflow",
                         "database_specific": {"severity": "HIGH"}},
               "CVE-2": {"id": "CVE-2", "summary": "dos",
                         "database_specific": {"severity": "MODERATE"}}})
    c = OsvClient(http=http, url="https://osv.test")
    comps = [{"name": "lib/tinyusb", "properties": _commit("a" * 40)},
             {"name": "lib/apriltag", "properties": _commit("36b9ba1")},   # not a full sha: skipped,
             {"name": "tool", "version": "2.1", "purl": "pkg:pypi/tool@2.1"},
             {"name": "openmv-sdk", "version": "1.7.3"},   # nothing that identifies it: skipped
             {"properties": _commit("bb22")}]               # skipped: no name
    out = advisor.REAL_OSV_SCAN(c, comps)
    assert [(f["component"], f["vuln_id"], f["severity"]) for f in out] == [
        ("lib/tinyusb", "CVE-1", "high"), ("lib/tinyusb", "CVE-2", "medium"),
        ("tool", "CVE-1", "high")]
    assert out[0]["summary"] == "overflow" and out[0]["version"] == ""
    assert http.gets.count("CVE-1") == 1                # detail cached across hits
    # never a bare name + version: that matched every distro's package of the same name
    assert all(set(q) == {"commit"} or "purl" in q["package"] or q["package"].get("ecosystem")
               for q in http.queries)


def test_micropython_is_looked_up_by_its_upstream_release_tag():
    """OpenMV builds MicroPython from a fork OSV never indexed, so its commit finds nothing;
    the fork is upstream v<version> plus patches, so the upstream tag is asked too. A vuln
    both queries report is one finding."""
    http = _StubHttp(hits={("git", "https://github.com/micropython/micropython", "v1.22.0"):
                           ["PYSEC-1"], ("commit", "f" * 40): ["PYSEC-1"]}, vulns={})
    comps = [{"name": "micropython", "version": "1.22.0", "properties": _commit("f" * 40)}]
    out = advisor.REAL_OSV_SCAN(OsvClient(http=http, url="u"), comps)
    assert [(f["component"], f["version"], f["vuln_id"]) for f in out] == [
        ("micropython", "1.22.0", "PYSEC-1")]
    assert {"commit": "f" * 40} in http.queries
    assert {"package": {"name": "https://github.com/micropython/micropython",
                        "ecosystem": "GIT"}, "version": "v1.22.0"} in http.queries


def test_osv_detail_404_falls_back_to_id():
    http = _StubHttp(hits={("commit", "c" * 40): ["GHSA-xyz"]}, vulns={})
    out = advisor.REAL_OSV_SCAN(OsvClient(http=http, url="u"),
                                [{"name": "x", "version": "1", "properties": _commit("c" * 40)}])
    assert out == [{"component": "x", "version": "1", "vuln_id": "GHSA-xyz",
                    "severity": "unknown", "summary": ""}]


def test_severity_mapping():
    assert _severity({"database_specific": {"severity": "CRITICAL"}}) == "critical"
    assert _severity({"database_specific": {"severity": "MODERATE"}}) == "medium"
    assert _severity({"database_specific": {"severity": "weird"}}) == "unknown"
    # real CVSS 3.1 base-score math, spec test vectors:
    def v(vec):
        return _severity({"severity": [{"type": "CVSS_V3", "score": vec}]})
    assert v("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H") == "critical"   # 9.8
    assert v("CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:U/C:H/I:N/A:N") == "medium"    # 6.5
    assert v("CVSS:3.1/AV:L/AC:L/PR:L/UI:N/S:U/C:H/I:N/A:N") == "medium"    # 5.5
    # the case the old shortcut got backwards: AC:H makes it LESS severe
    assert v("CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:L/I:N/A:N") == "low"       # 3.7
    # scope-changed branch
    assert v("CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:C/C:H/I:H/A:H") == "critical"  # 9.9
    assert v("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:N") == "unknown"   # 0.0
    assert v("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N") == "high"      # 7.5
    # malformed / non-v3 vectors fall through, never crash
    assert v("CVSS:3.1/AV:N") == "unknown"
    assert v("CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H") == "unknown"
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


def test_an_unreadable_sbom_keeps_the_standing_advisories(tmp_path):
    """A storage hiccup or a corrupt SBOM is not evidence the release is clean: scanning
    nothing would reconcile to zero findings and clear what is already known."""
    st = _state(tmp_path)
    st.metastore.upsert_advisories("r1", [{"vuln_id": "CVE-9", "component": "mbedtls",
                                           "version": "3.5.1", "severity": "high",
                                           "summary": "s"}], account_id="a")
    for key in ("sbom/gone.json", "sbom/bad.json"):
        out = advisor.scan_release(st, {"release_id": "r1", "account_id": "a",
                                        "sbom_key": key})
        assert out["cleared"] == 0
    st.storage.put("sbom/bad.json", b"not json", "application/json")
    advisor.scan_release(st, {"release_id": "r1", "account_id": "a", "sbom_key": "sbom/bad.json"})
    assert [r["vuln_id"] for r in st.metastore.list_advisories(account_id="a")] == ["CVE-9"]
    marks = [e["data"] for e in st.metastore.read_audit() if e["action"] == "advisory.scan"]
    assert marks and all(m == {"sbom_unreadable": True} for m in marks)


def test_a_short_osv_reply_fails_the_scan_instead_of_reporting_clean():
    class _Short(_StubHttp):
        def post(self, url, json=None):
            resp = super().post(url, json)
            resp._payload["results"] = resp._payload["results"][:-1]
            return resp
    http = _Short(hits={("commit", "2" * 40): ["CVE-1"]}, vulns={})
    c = OsvClient(http=http, url="https://osv.test")
    try:
        advisor.REAL_OSV_SCAN(c, [{"name": "mbedtls", "properties": _commit("1" * 40)},
                                 {"name": "lwip", "properties": _commit("2" * 40)}])
    except ValueError as e:
        assert "1 results for 2 queries" in str(e)
    else:
        raise AssertionError("a short reply must not scan clean")


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
    # a NEW finding is its own event (what a webhook subscriber acts on); a rescan that
    # finds the same thing again raises none
    found = [e for e in st.metastore.read_audit() if e["action"] == "advisory.found"]
    assert len(found) == 1 and found[0]["data"]["vuln_id"] == "CVE-9" and found[0]["data"]["severity"] == "high"
    advisor.scan_release(st, {"release_id": "r1", "account_id": "a", "sbom_key": "sbom/ok.json"})
    assert len([e for e in st.metastore.read_audit() if e["action"] == "advisory.found"]) == 1


def test_scheduler_disabled_at_zero_interval():
    from openmv_ota.server.cli import _schedule_advisory_scans

    class _App:                                  # would explode if a handler registered
        def on_event(self, *_):
            raise AssertionError("must not arm the loop")
    _schedule_advisory_scans(_App(), SimpleNamespace(advisory_scan_interval_s=0))


def test_only_releases_a_device_runs_are_scanned(tmp_path):
    """A release no device runs -- just published, only offered by a rollout, or left
    behind -- is not scanned, and findings it carried are cleared: the CVE list is what the
    fleet is actually exposed to."""
    st = _state(tmp_path)
    ms = st.metastore
    for rid, ver in (("r_run", "1.0.0"), ("r_offer", "1.1.0"), ("r_idle", "0.9.0")):
        ms.add_release(release_id=rid, product_id=7, product="p", version=ver,
                       payload_version=int(ver.replace(".", "")), min_platform_version=0,
                       image_sha256="ab", image_size=1, representations=[],
                       manifest_key="m" + rid, image_key="i" + rid, account_id="a",
                       sbom_key="sbom/" + rid)
        st.storage.put("sbom/" + rid, json.dumps({"components": [{"name": rid}]}).encode(),
                       "application/json")
        ms.upsert_advisories(rid, [{"vuln_id": "CVE-OLD", "component": "x", "version": "1",
                                    "severity": "low", "summary": ""}], account_id="a")
    ms.upsert_device(device_id="d1", product_id=7, current_version="1.0.0", account_id="a")
    ms.add_rollout(rollout_id="ro1", release_id="r_offer", product_id=7, cohort="__default__",
                   percent=100, account_id="a")
    scanned = []
    st.osv = SimpleNamespace(scan=lambda comps: scanned.append(comps[0]["name"]) or [])
    out = advisor.scan_account(st, "a")
    assert scanned == ["r_run"] and out["releases_scanned"] == 1
    # the running release's stale finding cleared by its own scan, the other two by scope
    assert ms.list_advisories(account_id="a") == []


_CVE9 = {"vuln_id": "CVE-9", "component": "mbedtls", "version": "3.5.1", "severity": "high",
         "summary": "s"}


def _actions(ms, action):
    return [e for e in ms.read_audit() if e["action"] == action]


def test_overlapping_reconciles_announce_each_transition_once(tmp_path, monkeypatch):
    """Two scans of one release can overlap (the scheduler's pass and an API scan, or two
    server instances): both read "not active yet" before either writes. The write decides,
    not that read -- the second reconcile reports nothing new and clears nothing twice."""
    ms = _state(tmp_path).metastore
    real = ms.query_all
    stale = {"rows": None}

    def query_all(sql, params=()):                # hand the reconcile a pre-captured read
        return stale["rows"] if stale["rows"] is not None else real(sql, params)
    monkeypatch.setattr(ms, "query_all", query_all)
    stale["rows"] = []                            # both scans read: nothing active
    assert len(ms.upsert_advisories("r1", [_CVE9], account_id="a")["new"]) == 1
    assert ms.upsert_advisories("r1", [_CVE9], account_id="a")["new"] == []
    stale["rows"] = real("SELECT * FROM advisories WHERE release_id = ?", ("r1",))
    first = ms.upsert_advisories("r1", [], account_id="a")
    assert first["cleared"] == 1 and first["cleared_findings"] == [
        {"release_id": "r1", "vuln_id": "CVE-9", "component": "mbedtls", "version": "3.5.1"}]
    assert ms.upsert_advisories("r1", [], account_id="a") == {
        "new": [], "cleared": 0, "cleared_findings": []}
    # and a cleared finding that comes back is new again
    stale["rows"] = None
    assert len(ms.upsert_advisories("r1", [_CVE9], account_id="a")["new"]) == 1


def test_a_finding_reported_twice_in_one_scan_is_one_finding(tmp_path):
    ms = _state(tmp_path).metastore
    out = ms.upsert_advisories("r1", [_CVE9, dict(_CVE9)], account_id="a")
    assert len(out["new"]) == 1


def test_concurrent_scans_of_a_release_record_found_once(tmp_path):
    """The live symptom: advisory.scan and advisory.found twice in the same second. Scans
    of one account now run one at a time, so the second sees the first's findings."""
    import threading

    st = _state(tmp_path)
    st.storage.put("sbom/ok.json", json.dumps({"components": [{"name": "mbedtls"}]}).encode(),
                   "application/json")
    gate = threading.Barrier(2, timeout=5)

    def scan(comps):
        try:
            gate.wait(timeout=0.3)                # both scans in OSV at once, if they could be
        except threading.BrokenBarrierError:
            pass
        return [_CVE9]
    st.osv = SimpleNamespace(scan=scan)
    rel = {"release_id": "r1", "account_id": "a", "sbom_key": "sbom/ok.json"}
    threads = [threading.Thread(target=advisor.scan_release, args=(st, rel)),
               threading.Thread(target=advisor.scan_release, args=(st, rel),
                                kwargs={"actor": "ci-token"})]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(_actions(st.metastore, "advisory.found")) == 1
    scans = _actions(st.metastore, "advisory.scan")
    assert sorted(e["data"]["new"] for e in scans) == [0, 1]


def test_a_cleared_finding_is_audited(tmp_path):
    """History shows resolution, not just discovery: a finding a later scan no longer
    reports, and one whose release no device runs any more, each get advisory.cleared."""
    st = _state(tmp_path)
    ms = st.metastore
    for rid, ver in (("r_old", "1.0.0"), ("r_new", "2.0.0")):
        ms.add_release(release_id=rid, product_id=7, product="p", version=ver,
                       payload_version=int(ver.replace(".", "")), min_platform_version=0,
                       image_sha256="ab", image_size=1, representations=[],
                       manifest_key="m" + rid, image_key="i" + rid, account_id="a",
                       sbom_key="sbom/" + rid)
        st.storage.put("sbom/" + rid, json.dumps({"components": [{"name": "x"}]}).encode(),
                       "application/json")
    ms.upsert_device(device_id="d1", product_id=7, current_version="1.0.0", account_id="a")
    ms.upsert_device(device_id="d2", product_id=7, current_version="2.0.0", account_id="a")
    other = dict(_CVE9, vuln_id="CVE-10")
    st.osv = SimpleNamespace(scan=lambda comps: [_CVE9, other])
    advisor.scan_account(st, "a")
    assert len(_actions(ms, "advisory.found")) == 4
    # CVE-10 withdrawn from the database; d1 moves to 2.0.0 so r_old leaves rotation
    st.osv = SimpleNamespace(scan=lambda comps: [_CVE9])
    ms.upsert_device(device_id="d1", product_id=7, current_version="2.0.0", account_id="a")
    advisor.scan_account(st, "a")
    got = sorted((e["entity_id"], e["data"]["vuln_id"], e["data"]["reason"], e["actor"])
                 for e in _actions(ms, "advisory.cleared"))
    assert got == [("r_new", "CVE-10", "not_reported", "scheduler"),
                   ("r_old", "CVE-10", "out_of_rotation", "scheduler"),
                   ("r_old", "CVE-9", "out_of_rotation", "scheduler")]
    cleared = _actions(ms, "advisory.cleared")[0]
    assert cleared["data"] == {"vuln_id": cleared["data"]["vuln_id"], "component": "mbedtls",
                               "version": "3.5.1", "reason": cleared["data"]["reason"]}
    assert cleared["product_id"] == 7
    # the scan rows keep their counts, consistent with the cleared events
    assert sum(e["data"]["cleared"] for e in _actions(ms, "advisory.scan")) == 3
    # nothing left to clear: a further scan announces nothing
    advisor.scan_account(st, "a")
    assert len(_actions(ms, "advisory.cleared")) == 3 and len(_actions(ms, "advisory.found")) == 4


def test_scheduled_scans_are_the_scheduler_api_scans_the_caller(tmp_path):
    st = _state(tmp_path)
    advisor.scan_release(st, {"release_id": "r1", "account_id": "a"})
    advisor.scan_release(st, {"release_id": "r1", "account_id": "a"}, actor="ci-token")
    assert [e["actor"] for e in _actions(st.metastore, "advisory.scan")] == [
        advisor.SCHEDULER, "ci-token"]
    assert advisor.SCHEDULER == "scheduler"

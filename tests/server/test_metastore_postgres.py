"""The metadata store against a REAL Postgres, with the REAL connection pool.

Opt-in (like the SoftHSM signer test): set ``OPENMV_OTA_PG_DSN`` to a disposable database and
these run; otherwise they skip. The rest of the suite runs on SQLite, which cannot see two
classes of production bug -- SQL that only one dialect accepts (``INSERT OR REPLACE`` was one,
and failed every new advisory on the production store), and races between pooled connections,
which the old single locked connection hid. Each test gets a fresh schema.

A local server with no install: ``pip install pgserver`` and
``pgserver.get_server(dir).get_uri()``.
"""
import os
import threading
import time
import uuid

import pytest

DSN = os.environ.get("OPENMV_OTA_PG_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="set OPENMV_OTA_PG_DSN to run against Postgres")


@pytest.fixture
def pg():
    import psycopg

    from openmv_ota.server.metastore import PostgresMetadataStore

    schema = "t_" + uuid.uuid4().hex[:12]
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("CREATE SCHEMA %s" % schema)
    sep = "&" if "?" in DSN else "?"
    store = PostgresMetadataStore(DSN + sep + "options=-csearch_path%3D" + schema, pool_size=8)
    store.migrate()
    try:
        yield store
    finally:
        store.close()
        with psycopg.connect(DSN, autocommit=True) as c:
            c.execute("DROP SCHEMA %s CASCADE" % schema)


def _hammer(n_threads, fn):
    errors, results = [], []
    lock = threading.Lock()

    def run(i):
        try:
            r = fn(i)
            with lock:
                results.append(r)
        except Exception as e:                       # noqa: BLE001 - collected and asserted
            with lock:
                errors.append(repr(e))

    ts = [threading.Thread(target=run, args=(i,)) for i in range(n_threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return results, errors


def test_migrates_a_fresh_database_on_one_pinned_connection(pg):
    from openmv_ota.server.metastore import _MIGRATIONS

    assert int(pg.get_meta("schema_version")) == len(_MIGRATIONS)
    assert pg.migrate() == len(_MIGRATIONS)                     # idempotent


def test_the_audit_chain_survives_concurrent_appenders(pg):
    """The race 7a801a9 fixed with the process lock -- which no longer spans pooled
    connections. The advisory lock has to hold it now."""
    results, errors = _hammer(16, lambda i: [pg.append_audit(actor="t%d" % i, action="x.y")
                                             for _ in range(10)])
    assert errors == []
    seqs = sorted(s for r in results for s in r)
    assert seqs == list(range(1, 161))
    assert pg.audit_chain_ok()


def test_publish_counters_never_repeat_under_concurrency(pg):
    pg.add_account("acct", "A")
    results, errors = _hammer(16, lambda i: [pg.next_publish_seq("acct") for _ in range(10)])
    assert errors == []
    got = sorted(n for r in results for n in r)
    assert got == list(range(1, 161))
    assert pg.next_publish_seq("nobody") is None


def test_declaring_one_cohort_concurrently_is_not_an_error(pg):
    _, errors = _hammer(16, lambda i: pg.declare_cohort("beta", "acct"))
    assert errors == []
    n = pg.query_one("SELECT COUNT(*) AS n FROM cohorts WHERE account_id = ? AND cohort = ?",
                     ("acct", "beta"))["n"]
    assert n == 1


def test_a_new_advisory_inserts_on_postgres(pg):
    finding = {"vuln_id": "CVE-1", "component": "mbedtls", "version": "3.0",
               "severity": "high", "summary": "s"}
    out = pg.upsert_advisories("rel-1", [finding], "acct")
    assert [a["vuln_id"] for a in out["new"]] == ["CVE-1"]
    again = pg.upsert_advisories("rel-1", [finding], "acct")          # a repeat refreshes
    assert again["new"] == []
    assert pg.upsert_advisories("rel-1", [], "acct")["cleared"] == 1   # and a drop clears


def test_concurrent_reconciles_announce_a_finding_once(pg):
    """Overlapping scans (the scheduler and an API call, or two instances) used to both
    read "not active" and both report the same finding as new -- duplicate audit events."""
    finding = {"vuln_id": "CVE-2", "component": "lwip", "version": "2.1",
               "severity": "low", "summary": ""}
    results, errors = _hammer(8, lambda i: pg.upsert_advisories("rel-2", [finding], "acct"))
    assert errors == []
    assert sum(len(r["new"]) for r in results) == 1
    results, errors = _hammer(8, lambda i: pg.upsert_advisories("rel-2", [], "acct"))
    assert errors == []
    assert sum(r["cleared"] for r in results) == 1


def test_two_webhook_workers_never_claim_the_same_delivery(pg):
    hook = pg.add_webhook(account_id="acct", url="https://example.invalid/h", events=["*"],
                          secret="s")
    for i in range(40):
        pg.enqueue_delivery(hook["webhook_id"], i + 1, "x.y")
    now = time.time() + 1
    results, errors = _hammer(8, lambda i: [d["delivery_id"]
                                            for d in pg.claim_due_deliveries(now, limit=40)])
    assert errors == []
    claimed = [d for r in results for d in r]
    assert len(claimed) == len(set(claimed)) == 40


def test_rate_counters_are_exact_under_concurrency(pg):
    """One atomic upsert per hit: concurrent instances can never under-count each other."""
    results, errors = _hammer(16, lambda i: [pg.rate_hit("10.0.0.1", 600) for _ in range(10)])
    assert errors == []
    assert sorted(n for r in results for n in r) == list(range(1, 161))
    assert pg.rate_sweep(660) == 1


def test_every_list_sort_runs_on_postgres(pg):
    """Each sortable list, by every sort key and both directions. `COLLATE NOCASE` is
    SQLite-only: on Postgres it failed the device, release, rollout and advisory lists
    sorted by name (a 500 on the rollout and cohort pages) while every SQLite test passed."""
    pg.add_release(release_id="rel_1", product_id=7, product="p", version="1.0.0",
                   payload_version=1, min_platform_version=0, image_sha256="ab", image_size=1,
                   representations=[{"format": "full", "size": 9}], manifest_key="m",
                   image_key="i", account_id="acct", display_name="Beta")
    pg.add_rollout(rollout_id="ro_1", release_id="rel_1", product_id=7, cohort="__default__",
                   percent=10, account_id="acct", display_name="alpha")
    pg.upsert_device(device_id="d1", product_id=7, current_version="1.0.0", account_id="acct")
    pg.upsert_advisories("rel_1", [{"vuln_id": "CVE-1", "component": "c", "version": "1",
                                    "severity": "low", "summary": ""}], "acct")
    pg.append_audit(actor="t", action="x.y", account_id="acct")
    for d in ("asc", "desc"):
        for key in pg.RELEASE_SORTS:
            assert pg.list_releases(account_id="acct", sort=key, direction=d)
        for key in pg.ROLLOUT_SORTS:
            assert pg.list_rollouts(account_id="acct", sort=key, direction=d)
        for key in pg.DEVICE_SORTS:
            assert pg.list_devices(account_id="acct", product_id=7, cohort="__default__",
                                   sort=key, direction=d)
        for key in pg.ADVISORY_SORTS:
            assert pg.list_advisories(account_id="acct", sort=key, direction=d)
        for key in pg.AUDIT_SORTS:
            assert pg.read_audit(account_id="acct", sort=key, direction=d)
    assert pg.get_release("rel_1")["download_size"] == 9              # v33 on Postgres too

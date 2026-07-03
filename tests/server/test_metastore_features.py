"""The metastore feature methods: releases, rollouts, the device registry, tokens, audit.

Everything groups by product_id (int) -- the manifest/check-in join key.
"""

from __future__ import annotations

from openmv_ota.server.metastore import SqliteMetadataStore

BID = 7          # a product_id


def _store() -> SqliteMetadataStore:
    s = SqliteMetadataStore(":memory:")
    s.migrate()
    return s


def _add_release(s, **kw):
    base = dict(release_id="rel1", product_id=BID, product="P", version="1.1.0",
                payload_version=0x01010000, min_platform_version=0, image_sha256="ab" * 32,
                image_size=1000, representations=[{"format": "full", "url": "x.img.gz", "size": 900}],
                manifest_key="m/rel1", image_key="i/rel1")
    s.add_release(**{**base, **kw})


# --- releases -------------------------------------------------------------------------------

def test_release_add_get_list_latest():
    s = _store()
    assert s.latest_release_payload_version(BID) is None
    _add_release(s)
    r = s.get_release("rel1")
    assert r["product_id"] == BID and r["representations"][0]["format"] == "full"
    _add_release(s, release_id="rel2", version="1.2.0", payload_version=0x01020000)
    assert [x["release_id"] for x in s.list_releases(BID)] == ["rel2", "rel1"]   # pv desc
    assert s.latest_release_payload_version(BID) == 0x01020000
    assert s.get_release("nope") is None and s.list_releases(999) == []


# --- rollouts -------------------------------------------------------------------------------

def test_rollout_lifecycle():
    s = _store()
    _add_release(s)
    s.add_rollout(rollout_id="ro1", release_id="rel1", product_id=BID, cohort="__default__",
                  percent=5)
    assert s.get_rollout("ro1")["state"] == "active" and s.get_rollout("ro1")["percent"] == 5
    assert s.active_rollout(BID, "__default__")["rollout_id"] == "ro1"
    s.update_rollout("ro1", percent=50)
    assert s.get_rollout("ro1")["percent"] == 50
    s.update_rollout("ro1", state="paused")
    assert s.active_rollout(BID, "__default__") is None
    assert [r["rollout_id"] for r in s.list_rollouts(BID)] == ["ro1"]
    assert [r["rollout_id"] for r in s.list_rollouts()] == ["ro1"]


def test_rollout_bump_counters():
    s = _store()
    s.add_rollout(rollout_id="ro1", release_id="rel1", product_id=1, cohort="c", percent=100)
    s.bump_rollout("ro1", attempted=1)
    s.bump_rollout("ro1", attempted=1, updated=1)
    s.bump_rollout("ro1", attempted=1, failures=1)
    r = s.get_rollout("ro1")
    assert (r["attempted"], r["updated"], r["failures"]) == (3, 1, 1)


# --- the device registry --------------------------------------------------------------------

def test_device_upsert_insert_then_update():
    s = _store()
    s.upsert_device(device_id="d1", product_id=BID, board="OPENMV_N6", current_version="1.0.0",
                    current_payload_version=0x01000000, slot="FRONT")
    d = s.get_device("d1")
    assert d["current_version"] == "1.0.0" and d["board"] == "OPENMV_N6"
    assert d["first_seen"] == d["last_seen"]
    first_seen = d["first_seen"]
    s.upsert_device(device_id="d1", product_id=BID, current_version="1.1.0",
                    current_payload_version=0x01010000, slot="FRONT", last_offered_release_id="rel1")
    d2 = s.get_device("d1")
    assert d2["current_version"] == "1.1.0" and d2["first_seen"] == first_seen
    assert d2["last_offered_release_id"] == "rel1"
    s.upsert_device(device_id="d1", product_id=BID, current_version="1.1.0")    # no offer
    assert s.get_device("d1")["last_offered_release_id"] == "rel1"            # COALESCE keeps it
    assert s.get_device("missing") is None


def test_device_cohort_not_reset_by_checkin():
    s = _store()
    s.upsert_device(device_id="d1", product_id=1, cohort="beta")
    s.upsert_device(device_id="d1", product_id=1, current_version="2.0.0")      # a plain check-in
    assert s.get_device("d1")["cohort"] == "beta"                             # admin cohort preserved


def test_list_devices_and_fleet_summary():
    s = _store()
    s.upsert_device(device_id="d1", product_id=1, current_version="1.0.0", slot="FRONT")
    s.upsert_device(device_id="d2", product_id=1, current_version="1.1.0", slot="FRONT")
    s.upsert_device(device_id="d3", product_id=1, current_version="1.0.0", slot="BACK")
    s.upsert_device(device_id="e1", product_id=2, current_version="9.0.0", slot="FRONT")
    assert {d["device_id"] for d in s.list_devices(1)} == {"d1", "d2", "d3"}
    assert len(s.list_devices()) == 4 and len(s.list_devices(1, limit=2)) == 2
    fs = s.fleet_summary(1)
    assert fs["total"] == 3
    assert fs["by_version"] == {"1.0.0": 2, "1.1.0": 1}
    assert fs["by_slot"] == {"FRONT": 2, "BACK": 1}
    assert s.fleet_summary()["total"] == 4


# --- admin tokens ---------------------------------------------------------------------------

def test_tokens():
    s = _store()
    assert s.count_tokens() == 0
    s.add_token("h1", "ci", ["publish", "manage"])
    t = s.get_token("h1")
    assert t["scopes"] == ["publish", "manage"] and t["revoked"] == 0
    s.revoke_token("h1")
    assert s.get_token("h1")["revoked"] == 1 and s.count_tokens() == 1
    s.add_token("h2", "viewer", [])
    assert s.get_token("h2")["scopes"] == []
    assert {t["name"] for t in s.list_tokens()} == {"ci", "viewer"}
    assert s.get_token("nope") is None


# --- the hash-chained audit log -------------------------------------------------------------

def test_audit_chain_and_read():
    s = _store()
    assert s.read_audit() == [] and s.audit_chain_ok() is True
    assert s.append_audit(actor="ci", action="release.publish", entity_type="release",
                          entity_id="rel1", data={"v": "1.1.0"}) == 1
    assert s.append_audit(actor="ci", action="rollout.create", entity_type="rollout",
                          entity_id="ro1") == 2
    e = s.read_audit()
    assert [x["action"] for x in e] == ["release.publish", "rollout.create"]
    assert e[0]["data"] == {"v": "1.1.0"} and e[1]["data"] == {}
    assert e[0]["prev_hash"] == "" and e[1]["prev_hash"] == e[0]["entry_hash"]
    assert s.audit_chain_ok() is True
    assert [x["seq"] for x in s.read_audit(since_seq=1)] == [2]
    assert len(s.read_audit(limit=1)) == 1


def test_audit_tamper_detected():
    s = _store()
    s.append_audit(actor="a", action="x")
    s.append_audit(actor="b", action="y")
    s.execute("UPDATE audit SET action = 'HACKED' WHERE seq = 1")
    assert s.audit_chain_ok() is False

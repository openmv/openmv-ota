"""``OPENMV_OTA_SERVE_UNREGISTERED_BOARDS``: a pre-launch switch that tracks the board types the
registry never registers (the Arduino boards) as registered devices, for every account.

Off (the default) nothing changes: such a board is served read-only -- offers scoped by the
account it claims, zero footprint. On, it gets what any registered device gets: a device row and
its enrollment, recorded feedback, Live + ingest grants, and the account's device limit."""

from __future__ import annotations

from fastapi.testclient import TestClient

from openmv_ota.server.app import create_app
from openmv_ota.server.metastore import SqliteMetadataStore
from openmv_ota.server.settings import ServerSettings
from openmv_ota.server.storage import LocalArtifactStorage
from openmv_ota.server.verify import Registration

BID = 7
ACCT = "acct_example"                 # a placeholder, not a real account
DEV = "ARDUINO_GIGA:dev1"             # the board-qualified id a check-in is stored under


class _ArduinoRegistry:
    """The registry's verdict for an Arduino board: structurally never registered."""

    def verify(self, board, device_id):
        return Registration(False, unregistered_board_type=True)


def _app(tmp_path, *, on):
    store = SqliteMetadataStore(str(tmp_path / "ota.db"))
    store.migrate()
    store.set_meta("capability_secret", "x")
    store.add_account(ACCT, "Example")
    store.add_release(release_id="rel1", product_id=BID, product="P", version="2.0.0",
                      payload_version=0x02000000, min_platform_version=0, image_sha256="ab" * 32,
                      image_size=3, representations=[{"format": "full", "url": "x.img.gz",
                                                      "size": 3}],
                      manifest_key="m/rel1", image_key="i/rel1", account_id=ACCT)
    store.add_rollout(rollout_id="ro1", release_id="rel1", product_id=BID, cohort="__default__",
                      percent=100, account_id=ACCT)
    storage = LocalArtifactStorage(str(tmp_path / "blobs"))
    storage.put("m/rel1", b"MANI", "application/octet-stream")
    storage.put("i/rel1", b"IMG", "application/gzip")
    settings = ServerSettings(base_url="https://ota.test", swd_ids_verify_url="u",
                              swd_ids_verify_token="t", poll_jitter=0,
                              live_relay_url="https://live.test", live_token_secret="s3cret",
                              datalake_url="https://data.test", datalake_token_secret="dl",
                              serve_unregistered_boards=on)
    app = create_app(settings, storage=storage, metastore=store, verifier=_ArduinoRegistry())
    return TestClient(app), store


def _check(c, dev="dev1", account=ACCT):
    return c.post("/api/v1/check", json={"device_id": dev, "product_id": BID,
                                         "payload_version": 0x01000000, "board": "ARDUINO_GIGA",
                                         "account_id": account, "streams": ["0"]}).json()


def _feedback(c):
    return c.post("/api/v1/feedback", json={
        "device_id": "dev1", "product_id": BID, "board": "ARDUINO_GIGA", "release_id": "rel1",
        "status": "installed", "account_id": ACCT}).json()


def test_off_by_default():
    assert ServerSettings().serve_unregistered_boards is False


def test_the_env_var_parses_as_a_boolean(monkeypatch):
    for raw, want in (("1", True), ("true", True), ("YES", True), ("0", False), ("off", False)):
        monkeypatch.setenv("OPENMV_OTA_SERVE_UNREGISTERED_BOARDS", raw)
        assert ServerSettings().serve_unregistered_boards is want, raw


def test_off_an_arduino_board_is_served_read_only(tmp_path):
    c, store = _app(tmp_path, on=False)
    r = _check(c)
    assert r["update"] is True                               # offers still work (account-scoped)
    assert "live" not in r and "ingest" not in r             # no grants
    assert store.get_device(DEV) is None                     # zero footprint
    assert store.get_rollout("ro1")["attempted"] == 0
    assert _feedback(c) == {"ok": False}
    assert store.deployment_counts("rel1") == {"installed": 0, "failed": 0}


def test_on_an_arduino_board_is_a_registered_device(tmp_path):
    c, store = _app(tmp_path, on=True)
    r = _check(c)
    assert r["update"] is True
    assert "live" in r and "ingest" in r                     # Live + datalake grants
    row = store.get_device(DEV)
    assert row is not None and row["account_id"] == ACCT and row["board"] == "ARDUINO_GIGA"
    assert store.get_rollout("ro1")["attempted"] == 1        # rollout accounting
    events = [e["action"] for e in store.read_audit(account_id=ACCT)]
    assert "device.enrolled" in events
    assert _feedback(c) == {"ok": True}
    assert store.deployment_counts("rel1") == {"installed": 1, "failed": 0}


def test_on_the_accounts_device_limit_counts_them(tmp_path):
    c, store = _app(tmp_path, on=True)
    store.set_device_limit(ACCT, 1)
    _check(c, dev="dev1")
    assert _check(c, dev="dev2") == {"update": False, "poll_after_s": 3600}   # over the limit
    assert store.get_device("ARDUINO_GIGA:dev2") is None
    assert store.device_count(ACCT) == 1


def test_on_it_applies_to_every_account(tmp_path):
    c, store = _app(tmp_path, on=True)
    r = _check(c, account="acct_other")
    assert r["update"] is False                              # nothing published under that account
    assert store.get_device(DEV)["account_id"] == "acct_other"


def test_the_server_says_so_at_startup(tmp_path, capsys):
    (tmp_path / "on").mkdir()
    (tmp_path / "off").mkdir()
    _app(tmp_path / "on", on=True)
    assert "serve_unregistered_boards is ON" in capsys.readouterr().err
    _app(tmp_path / "off", on=False)
    assert "serve_unregistered_boards" not in capsys.readouterr().err

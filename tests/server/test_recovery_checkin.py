"""The check-in firmware-resident recovery sends is one ``/api/v1/check`` accepts and answers.

Recovery builds its request from the firmware's constants alone (there is no image, so no
system.json), byte for byte on the wire; this posts exactly those bytes at the real app and feeds
the answer back through recovery's own parser."""

from __future__ import annotations

from fastapi.testclient import TestClient

from openmv_ota.build.device import openmv_recovery as rec
from openmv_ota.server.app import create_app
from openmv_ota.server.metastore import SqliteMetadataStore
from openmv_ota.server.settings import ServerSettings
from openmv_ota.server.storage import LocalArtifactStorage
from openmv_ota.server.verify import Registration
from tests.server import camera

PID = 7
ACCT = "acct_example"                 # a placeholder, not a real account


class _Registry:
    def verify(self, board, device_id):
        return Registration(bool(board and device_id))


class _Cfg:
    PRODUCT_ID = PID
    ACCOUNT_ID = ACCT
    BOARD = "OPENMV4"


def _client(tmp_path, *, rollout=True):
    store = SqliteMetadataStore(str(tmp_path / "ota.db"))
    store.migrate()
    store.set_meta("capability_secret", "x")
    store.add_account(ACCT, "Example")
    store.add_release(release_id="rel1", product_id=PID, product="P", version="2.0.0",
                      payload_version=0x02000000, min_platform_version=0, image_sha256="ab" * 32,
                      image_size=3, representations=[{"format": "full", "url": "x.img.gz",
                                                      "size": 3}],
                      manifest_key="m/rel1", image_key="i/rel1", account_id=ACCT)
    if rollout:
        store.add_rollout(rollout_id="ro1", release_id="rel1", product_id=PID,
                          cohort="__default__", percent=100, account_id=ACCT)
    storage = LocalArtifactStorage(str(tmp_path / "blobs"))
    storage.put("m/rel1", b"MANI", "application/octet-stream")
    settings = ServerSettings(base_url="https://ota.test", swd_ids_verify_url="u",
                              swd_ids_verify_token="t", poll_jitter=0)
    app = create_app(settings, storage=storage, metastore=store, verifier=_Registry())
    return TestClient(app), store


def _post(c, board_id="ABC123", sign=None):
    """Recovery's request, as bytes, at the app: the body and the headers it sets."""
    req = rec.checkin_request(_Cfg, "ota.test", 443, board_id, "stm32", b"\x01", sign)
    head, _, body = req.partition(b"\r\n\r\n")
    assert head.startswith(b"POST /api/v1/check ")
    headers = {"Content-Type": "application/json"}
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.decode().partition(": ")
        if name == "X-OMV-Signature":
            headers[name] = value
    return c.post("/api/v1/check", content=body, headers=headers)


def test_a_recovering_device_is_offered_the_release(tmp_path):
    """Before its clock is set, recovery's check-in goes unsigned: it gets what any unsigned
    check-in gets -- the update offer, and no fleet row."""
    c, store = _client(tmp_path)
    r = _post(c)
    assert r.status_code == 200
    url = rec.offered(r.status_code, r.content)
    assert url and url.startswith("https://ota.test/d/")   # a manifest URL, not the server root
    assert store.count_devices() == 0


def test_a_signed_recovery_puts_the_camera_on_the_fleet_view_as_recovering(tmp_path):
    """Once its clock is set, recovery signs with the camera's identity key: the offer, and a
    fleet row saying the camera is recovering."""
    c, store = _client(tmp_path)
    key = camera.register(store, "OPENMV4:ABC123", ACCT)
    r = _post(c, sign=lambda body: camera.sign(key, body))
    assert rec.offered(r.status_code, r.content)
    row = store.get_device("OPENMV4:ABC123")
    assert row["fallback_reason"] == "recovery" and row["account_id"] == ACCT
    assert store.get_registration("OPENMV4:ABC123")["last_signed_at"]


def test_a_recovery_signed_by_the_wrong_key_gets_nothing(tmp_path):
    c, store = _client(tmp_path)
    camera.register(store, "OPENMV4:ABC123", ACCT)
    other = camera.key_for("someone else")
    r = _post(c, sign=lambda body: camera.sign(other, body))
    assert rec.offered(r.status_code, r.content) is None and store.count_devices() == 0


def test_nothing_on_offer_is_an_answer_recovery_waits_on(tmp_path):
    c, _ = _client(tmp_path, rollout=False)
    r = _post(c)
    assert r.status_code == 200 and rec.offered(r.status_code, r.content) is None


def test_a_device_without_an_id_is_served_nothing(tmp_path):
    c, _ = _client(tmp_path)
    r = _post(c, board_id="")
    # the empty id reaches the registration gate as unregistered: served nothing, not offered
    assert rec.offered(r.status_code, r.content) is None

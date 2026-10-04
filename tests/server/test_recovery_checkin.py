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


def _post(c, board_id="ABC123"):
    """Recovery's request, as bytes, at the app: the body and the headers it sets."""
    req = rec.checkin_request(_Cfg, "ota.test", 443, board_id, "stm32", b"\x01")
    head, _, body = req.partition(b"\r\n\r\n")
    assert head.startswith(b"POST /api/v1/check ")
    return c.post("/api/v1/check", content=body, headers={"Content-Type": "application/json"})


def test_a_recovering_device_is_offered_the_release_and_shows_as_recovering(tmp_path):
    c, store = _client(tmp_path)
    r = _post(c)
    assert r.status_code == 200
    url = rec.offered(r.status_code, r.content)
    assert url and url.startswith("https://ota.test/d/")   # a manifest URL, not the server root
    dev = store.get_device("OPENMV4:ABC123")                # board-qualified: BOARD was sent
    assert dev["fallback_reason"] == "recovery" and dev["current_payload_version"] == 0


def test_nothing_on_offer_is_an_answer_recovery_waits_on(tmp_path):
    c, _ = _client(tmp_path, rollout=False)
    r = _post(c)
    assert r.status_code == 200 and rec.offered(r.status_code, r.content) is None


def test_a_device_without_an_id_is_served_nothing(tmp_path):
    c, _ = _client(tmp_path)
    r = _post(c, board_id="")
    # the empty id reaches the registration gate as unregistered: served nothing, not offered
    assert rec.offered(r.status_code, r.content) is None

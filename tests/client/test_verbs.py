"""The client verbs end-to-end against a real server (via an injected TestClient)."""

from __future__ import annotations

import gzip
import hashlib

import pytest
from fastapi.testclient import TestClient

from openmv_ota.cli import main
from openmv_ota.client import cli as client_cli
from openmv_ota.client.api import Api
from openmv_ota.ota.algorithms import ES256
from openmv_ota.ota.manifest import Manifest, pack_manifest
from openmv_ota.server.app import create_app
from openmv_ota.server.auth import hash_token
from openmv_ota.server.metastore import SqliteMetadataStore
from openmv_ota.server.settings import ServerSettings
from openmv_ota.server.storage import LocalArtifactStorage
from openmv_ota.server.verify import Registration

BID = 7


class _Verifier:
    def verify(self, board, device_id):
        return Registration(True)


def _server(tmp_path, scopes=("release:write", "rollout:control", "fleet:read")):
    store = SqliteMetadataStore(str(tmp_path / "ota.db"))
    store.migrate()
    store.set_meta("cohort_salt", "x")
    store.add_token(hash_token("tok"), "ci", list(scopes))
    app = create_app(ServerSettings(base_url="https://ota.test", swd_ids_verify_url="u",
                                    swd_ids_verify_token="t"),
                     metastore=store, storage=LocalArtifactStorage(str(tmp_path / "blobs")),
                     verifier=_Verifier())
    return app, store


@pytest.fixture
def wired(tmp_path, monkeypatch):
    app, store = _server(tmp_path)
    tc = TestClient(app)
    monkeypatch.setattr(client_cli, "_make_api", lambda cfg: Api(cfg, client=tc))
    monkeypatch.setenv("OPENMV_OTA_SERVER", "https://ota.test")
    monkeypatch.setenv("OPENMV_OTA_TOKEN", "tok")
    return store, tmp_path


def _build_release(project, board="OPENMV_N6", pv=0x02000000):
    build = project / "build"
    build.mkdir(parents=True, exist_ok=True)
    img = b"\xA5" * 64
    image_gz = gzip.compress(img, mtime=0)
    body = {"schema": 1, "product_id": BID, "product": "P", "version": "2.0.0", "payload_version": pv,
            "min_platform_version": 0, "size": len(img), "sha256": hashlib.sha256(img).hexdigest(),
            "representations": [{"format": "full", "url": "%s-ota.img.gz" % board,
                                 "size": len(image_gz)}]}
    manifest = pack_manifest(Manifest(body=body, key_id=0x0100, sig_alg=ES256,
                                      signature=b"\x00" * 64))
    (build / ("%s-manifest.bin" % board)).write_bytes(manifest)
    (build / ("%s-ota.img.gz" % board)).write_bytes(image_gz)
    return build


def test_publish_and_rollout(wired, tmp_path, capsys):
    store, _ = wired
    project = tmp_path / "proj"
    _build_release(project)
    assert main(["client", "publish", str(project), "-b", "OPENMV_N6", "--rollout", "beta:5"]) == 0
    out = capsys.readouterr().out
    assert "published rel_" in out and "rollout ro_" in out
    releases = store.list_releases(BID)
    assert len(releases) == 1 and store.list_rollouts(BID)[0]["cohort"] == "beta"


def test_publish_missing_artifacts(wired, tmp_path, capsys):
    store, _ = wired
    assert main(["client", "publish", str(tmp_path / "empty"), "-b", "OPENMV_N6"]) == 2
    assert "no built release" in capsys.readouterr().err


def test_publish_bad_rollout_spec(wired, tmp_path, capsys):
    store, _ = wired
    project = tmp_path / "proj"
    _build_release(project)
    assert main(["client", "publish", str(project), "-b", "OPENMV_N6", "--rollout", "beta:x"]) == 2
    assert "bad --rollout" in capsys.readouterr().err


def test_publish_server_rejects_republish(wired, tmp_path, capsys):
    store, _ = wired
    project = tmp_path / "proj"
    _build_release(project, pv=0x02000000)
    assert main(["client", "publish", str(project), "-b", "OPENMV_N6"]) == 0
    capsys.readouterr()
    assert main(["client", "publish", str(project), "-b", "OPENMV_N6"]) == 1   # same pv -> 409
    assert "409" in capsys.readouterr().err


def _publish(store, tmp_path):
    project = tmp_path / "p2"
    _build_release(project)
    main(["client", "publish", str(project), "-b", "OPENMV_N6", "--rollout", "beta:5"])
    return store.list_rollouts(BID)[0]["rollout_id"]


def test_rollout_raise_pause_resume_rollback(wired, tmp_path, capsys):
    store, _ = wired
    rid = _publish(store, tmp_path)
    capsys.readouterr()
    assert main(["client", "rollout", "raise", "--id", rid, "--percent", "50"]) == 0
    assert store.get_rollout(rid)["percent"] == 50
    assert main(["client", "rollout", "pause", "--id", rid]) == 0
    assert store.get_rollout(rid)["state"] == "paused"
    assert main(["client", "rollout", "resume", "--id", rid]) == 0
    assert store.get_rollout(rid)["state"] == "active"
    assert main(["client", "rollout", "rollback", "--id", rid]) == 0
    assert store.get_rollout(rid)["state"] == "rolled_back"
    assert "rolled_back" in capsys.readouterr().out


def test_rollout_server_error_surfaced(wired, tmp_path, capsys):
    store, _ = wired
    assert main(["client", "rollout", "pause", "--id", "nope"]) == 1   # 404 -> exit 1
    assert "404" in capsys.readouterr().err


def test_fleet_devices_audit(wired, tmp_path, capsys):
    import json
    store, _ = wired
    store.upsert_device(device_id="d1", product_id=BID, current_version="1.0.0", slot="FRONT")
    store.append_audit(actor="ci", action="release.publish")
    assert main(["client", "fleet"]) == 0
    assert json.loads(capsys.readouterr().out)["total"] == 1
    assert main(["client", "devices", "--product-id", str(BID)]) == 0
    assert json.loads(capsys.readouterr().out)["devices"][0]["device_id"] == "d1"
    store.add_release(release_id="rel1", product_id=BID, product="P", version="2.0.0",
                      payload_version=0x02000000, min_platform_version=0, image_sha256="ab" * 32,
                      image_size=1, representations=[{"format": "full", "url": "x", "size": 1}],
                      manifest_key="m", image_key="i")
    assert main(["client", "releases"]) == 0
    assert json.loads(capsys.readouterr().out)["releases"][0]["release_id"] == "rel1"
    assert main(["client", "audit"]) == 0
    assert json.loads(capsys.readouterr().out)["events"][0]["action"] == "release.publish"


def test_cohort_list_and_assign(wired, tmp_path, capsys):
    import json
    store, _ = wired
    store.upsert_device(device_id="d1", product_id=BID)
    assert main(["client", "cohort", "assign", "--cohort", "beta", "--device", "d1"]) == 0
    assert "assigned 1/1 device(s) to cohort beta" in capsys.readouterr().out
    assert main(["client", "cohort", "list"]) == 0
    assert json.loads(capsys.readouterr().out) == {"cohorts": [{"cohort": "beta", "devices": 1}]}


def test_cohort_error_surfaced(tmp_path, monkeypatch, capsys):
    app, store = _server(tmp_path, scopes=("fleet:read",))     # token can't control -> assign 403s
    tc = TestClient(app)
    monkeypatch.setattr(client_cli, "_make_api", lambda cfg: Api(cfg, client=tc))
    monkeypatch.setenv("OPENMV_OTA_SERVER", "https://ota.test")
    monkeypatch.setenv("OPENMV_OTA_TOKEN", "tok")
    assert main(["client", "cohort", "assign", "--cohort", "b", "--device", "d1"]) == 1
    assert "403" in capsys.readouterr().err


def test_pin_device_and_cohort(wired, tmp_path, capsys):
    store, _ = wired
    store.upsert_device(device_id="d1", product_id=BID)
    assert main(["client", "pin", "device", "--id", "d1", "--release", "rel9"]) == 0
    assert "device d1 pinned to rel9" in capsys.readouterr().out
    assert store.get_device("d1")["pinned_release_id"] == "rel9"
    assert main(["client", "pin", "device", "--id", "d1", "--clear"]) == 0
    assert "(unpinned)" in capsys.readouterr().out
    assert main(["client", "pin", "cohort", "--product-id", str(BID),
                 "--cohort", "beta", "--release", "rel9"]) == 0
    assert "cohort beta pinned to rel9" in capsys.readouterr().out
    assert store.get_cohort_pin(BID, "beta") == "rel9"


def test_bind_device(wired, tmp_path, capsys):
    store, _ = wired
    store.upsert_device(device_id="d1", product_id=BID)
    assert main(["client", "bind", "--id", "d1"]) == 0
    assert "device d1 bound to" in capsys.readouterr().out
    assert store.device_account("d1")["source"] == "admin"


def _wire_super_admin(tmp_path, monkeypatch, scopes):
    app, store = _server(tmp_path, scopes=scopes)
    tc = TestClient(app)
    monkeypatch.setattr(client_cli, "_make_api", lambda cfg: Api(cfg, client=tc))
    monkeypatch.setenv("OPENMV_OTA_SERVER", "https://ota.test")
    monkeypatch.setenv("OPENMV_OTA_TOKEN", "tok")
    return store


def test_account_create_and_list(tmp_path, monkeypatch, capsys):
    _wire_super_admin(tmp_path, monkeypatch, scopes=("account:admin",))
    assert main(["client", "account", "create", "--name", "DroneCo"]) == 0
    out = capsys.readouterr().out
    assert "created" in out and "admin token" in out
    assert main(["client", "account", "list"]) == 0
    assert "DroneCo" in capsys.readouterr().out


def test_account_error_surfaced(tmp_path, monkeypatch, capsys):
    _wire_super_admin(tmp_path, monkeypatch, scopes=("fleet:read",))    # no account:admin -> 403
    assert main(["client", "account", "create", "--name", "X"]) == 1
    assert "403" in capsys.readouterr().err


def test_bind_error_surfaced(tmp_path, monkeypatch, capsys):
    app, store = _server(tmp_path, scopes=("fleet:read",))     # token can't control -> bind 403s
    tc = TestClient(app)
    monkeypatch.setattr(client_cli, "_make_api", lambda cfg: Api(cfg, client=tc))
    monkeypatch.setenv("OPENMV_OTA_SERVER", "https://ota.test")
    monkeypatch.setenv("OPENMV_OTA_TOKEN", "tok")
    assert main(["client", "bind", "--id", "d1"]) == 1
    assert "403" in capsys.readouterr().err


def test_pin_error_surfaced(tmp_path, monkeypatch, capsys):
    app, store = _server(tmp_path, scopes=("fleet:read",))     # token can't control -> pin 403s
    store.upsert_device(device_id="d1", product_id=BID)
    tc = TestClient(app)
    monkeypatch.setattr(client_cli, "_make_api", lambda cfg: Api(cfg, client=tc))
    monkeypatch.setenv("OPENMV_OTA_SERVER", "https://ota.test")
    monkeypatch.setenv("OPENMV_OTA_TOKEN", "tok")
    assert main(["client", "pin", "device", "--id", "d1", "--release", "r"]) == 1
    assert "403" in capsys.readouterr().err


def test_missing_creds(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OPENMV_OTA_SERVER", raising=False)
    monkeypatch.delenv("OPENMV_OTA_TOKEN", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))       # no saved profile
    assert main(["client", "fleet"]) == 2
    assert "no server URL" in capsys.readouterr().err


def test_client_no_subcommand(capsys):
    assert main(["client"]) == 1

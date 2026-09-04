"""The client verbs end-to-end against a real server (via an injected TestClient)."""

from __future__ import annotations

import gzip
import hashlib
import json

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


def _server(tmp_path, scopes=("publish", "manage", "observe")):
    store = SqliteMetadataStore(str(tmp_path / "ota.db"))
    store.migrate()
    store.set_meta("capability_secret", "x")
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


def _rewrite_manifest(build, board, reps):
    """Re-sign-shape the built manifest with a different representation set."""
    from openmv_ota.ota.manifest import Manifest, pack_manifest, parse_manifest

    path = build / ("%s-manifest.bin" % board)
    body = parse_manifest(path.read_bytes()).body
    body["representations"] = reps
    path.write_bytes(pack_manifest(Manifest(body=body, key_id=0x0100, sig_alg=ES256,
                                            signature=b"\x00" * 64)))


def test_publish_and_rollout(wired, tmp_path, capsys):
    store, _ = wired
    project = tmp_path / "proj"
    _build_release(project)
    assert main(["client", "release", "publish", str(project), "-b", "OPENMV_N6", "--cohort", "beta", "--percent", "5", "--name", "First light"]) == 0
    out = capsys.readouterr().out
    assert "published rel_" in out and "rollout ro_" in out
    releases = store.list_releases(BID)
    assert len(releases) == 1 and store.list_rollouts(BID)[0]["cohort"] == "beta"
    assert releases[0]["key_id"] == 0x0100      # the manifest's signing key, recorded
    assert releases[0]["display_name"] == "First light"    # --name rides with publish


def test_publish_uploads_every_delta_the_manifest_declares(wired, tmp_path, capsys):
    """The manifest is the authority on which artifacts belong to a release. A release now
    ships one delta per base version, so there is no single filename to guess at -- and a
    client that guessed would silently publish a release missing most of its deltas."""
    from openmv_ota.ota.delta import make_delta
    from openmv_ota.ota.manifest import DELTA_FORMAT

    store, _ = wired
    project = tmp_path / "proj"
    build = _build_release(project)
    board = "OPENMV_N6"
    image = gzip.decompress((build / ("%s-ota.img.gz" % board)).read_bytes())
    reps = [{"format": "full", "url": "%s-ota.img.gz" % board, "size": 10}]
    for base, filler in (("1.0.0", b"\x00"), ("1.1.0", b"\x11")):
        name = "%s-ota.delta-%s.gz" % (board, base)
        (build / name).write_bytes(gzip.compress(make_delta(filler * 128, image), mtime=0))
        reps.append({"format": DELTA_FORMAT, "url": name, "size": 1,
                     "base_payload_version": 0x01000000})
    _rewrite_manifest(build, board, reps)

    assert main(["client", "release", "publish", str(project), "-b", board]) == 0
    rel = store.list_releases(BID)[0]
    assert len([r for r in rel["representations"] if r["format"] == DELTA_FORMAT]) == 2


def test_publish_errors_when_a_declared_delta_is_missing(wired, tmp_path, capsys):
    """Caught locally, where the fix is, rather than as a 400 from the server."""
    from openmv_ota.ota.manifest import DELTA_FORMAT

    store, _ = wired
    project = tmp_path / "proj"
    build = _build_release(project)
    board = "OPENMV_N6"
    _rewrite_manifest(build, board, [
        {"format": "full", "url": "%s-ota.img.gz" % board, "size": 10},
        {"format": DELTA_FORMAT, "url": "%s-ota.delta-9.9.9.gz" % board, "size": 1,
         "base_payload_version": 0x01000000}])
    assert main(["client", "release", "publish", str(project), "-b", board]) == 2
    assert "declares delta" in capsys.readouterr().err


def test_publish_rejects_an_unreadable_manifest(wired, tmp_path, capsys):
    store, _ = wired
    project = tmp_path / "proj"
    build = _build_release(project)
    (build / "OPENMV_N6-manifest.bin").write_bytes(b"not a manifest at all")
    assert main(["client", "release", "publish", str(project), "-b", "OPENMV_N6"]) == 2
    assert "unreadable manifest" in capsys.readouterr().err


def test_bases_downloads_retained_images_for_the_build_to_diff_against(wired, tmp_path, capsys):
    """The retention loop, end to end: publish a release, then pull its image back as a delta
    base. The build machine keeps nothing -- the server does -- and the file lands with the
    name `build ota-romfs --delta-from <dir>` looks for."""
    store, _ = wired
    project = tmp_path / "proj"
    _build_release(project)
    assert main(["client", "release", "publish", str(project), "-b", "OPENMV_N6"]) == 0
    capsys.readouterr()

    dest = tmp_path / "bases"
    assert main(["client", "release", "bases", "-b", "OPENMV_N6", "-o", str(dest)]) == 0
    written = sorted(p.name for p in dest.iterdir())
    assert written == ["OPENMV_N6-base-2.0.0.img.gz"]
    # ...and it is the published image byte-for-byte, which is what a delta must diff against
    assert (dest / written[0]).read_bytes() == (
        project / "build" / "OPENMV_N6-ota.img.gz").read_bytes()


def test_bases_reports_when_there_is_no_history_yet(wired, tmp_path, capsys):
    store, _ = wired
    assert main(["client", "release", "bases", "-b", "OPENMV_N6", "-o", str(tmp_path / "b")]) == 2
    assert "no retained releases" in capsys.readouterr().err


def test_prune_deletes_a_release_s_objects(wired, tmp_path, capsys):
    """Retention is unbounded, so removing a release's bytes is a deliberate operator action.
    The release row -- audit trail and version history -- is untouched."""
    store, _ = wired
    project = tmp_path / "proj"
    _build_release(project)
    assert main(["client", "release", "publish", str(project), "-b", "OPENMV_N6"]) == 0
    rel_id = store.list_releases(BID)[0]["release_id"]
    capsys.readouterr()

    assert main(["client", "release", "prune", "--release-id", rel_id]) == 0
    assert "deleted 2 object(s)" in capsys.readouterr().out    # manifest + image
    assert store.get_release(rel_id) is not None               # history survives


def test_prune_refuses_a_release_a_rollout_still_offers(wired, tmp_path, capsys):
    store, _ = wired
    project = tmp_path / "proj"
    _build_release(project)
    assert main(["client", "release", "publish", str(project), "-b", "OPENMV_N6",
                 "--cohort", "beta", "--percent", "100"]) == 0
    rel_id = store.list_releases(BID)[0]["release_id"]
    capsys.readouterr()
    assert main(["client", "release", "prune", "--release-id", rel_id]) == 1
    assert "still being offered" in capsys.readouterr().err
    # ...and --force is the explicit override
    assert main(["client", "release", "prune", "--release-id", rel_id, "--force"]) == 0


def test_publish_missing_artifacts(wired, tmp_path, capsys):
    store, _ = wired
    assert main(["client", "release", "publish", str(tmp_path / "empty"), "-b", "OPENMV_N6"]) == 2
    assert "no built release" in capsys.readouterr().err


def test_publish_bad_rollout_spec(wired, tmp_path, capsys):
    store, _ = wired
    project = tmp_path / "proj"
    _build_release(project)
    assert main(["client", "release", "publish", str(project), "-b", "OPENMV_N6", "--cohort", "beta"]) == 2
    assert "--cohort stages a rollout only with --percent" in capsys.readouterr().err


def test_publish_server_rejects_republish(wired, tmp_path, capsys):
    store, _ = wired
    project = tmp_path / "proj"
    _build_release(project, pv=0x02000000)
    assert main(["client", "release", "publish", str(project), "-b", "OPENMV_N6"]) == 0
    capsys.readouterr()
    assert main(["client", "release", "publish", str(project), "-b", "OPENMV_N6"]) == 1   # same pv -> 409
    assert "409" in capsys.readouterr().err


def _publish(store, tmp_path):
    project = tmp_path / "p2"
    _build_release(project)
    main(["client", "release", "publish", str(project), "-b", "OPENMV_N6", "--cohort", "beta", "--percent", "5"])
    return store.list_rollouts(BID)[0]["rollout_id"]


def test_rollout_raise_pause_resume_rollback(wired, tmp_path, capsys):
    store, _ = wired
    rid = _publish(store, tmp_path)
    capsys.readouterr()
    assert main(["client", "rollout", "raise", "--rollout-id", rid, "50"]) == 0
    assert store.get_rollout(rid)["percent"] == 50
    assert main(["client", "rollout", "pause", "--rollout-id", rid]) == 0
    assert store.get_rollout(rid)["state"] == "paused"
    assert main(["client", "rollout", "resume", "--rollout-id", rid]) == 0
    assert store.get_rollout(rid)["state"] == "active"
    assert main(["client", "rollout", "stop", "--rollout-id", rid]) == 0
    assert store.get_rollout(rid)["state"] == "stopped"
    assert "stopped" in capsys.readouterr().out


def test_rollout_server_error_surfaced(wired, tmp_path, capsys):
    store, _ = wired
    assert main(["client", "rollout", "pause", "--rollout-id", "nope"]) == 1   # 404 -> exit 1
    assert "404" in capsys.readouterr().err


def test_fleet_devices_audit(wired, tmp_path, capsys):
    import json
    store, _ = wired
    store.upsert_device(device_id="d1", product_id=BID, current_version="1.0.0", slot="FRONT")
    store.append_audit(actor="ci", action="release.publish")
    assert main(["client", "fleet"]) == 0
    assert json.loads(capsys.readouterr().out)["total"] == 1
    assert main(["client", "device", "list", "--product-id", str(BID)]) == 0
    assert json.loads(capsys.readouterr().out)["devices"][0]["device_id"] == "d1"
    store.add_release(release_id="rel1", product_id=BID, product="P", version="2.0.0",
                      payload_version=0x02000000, min_platform_version=0, image_sha256="ab" * 32,
                      image_size=1, representations=[{"format": "full", "url": "x", "size": 1}],
                      manifest_key="m", image_key="i")
    assert main(["client", "release", "list"]) == 0
    assert json.loads(capsys.readouterr().out)["releases"][0]["release_id"] == "rel1"
    assert main(["client", "audit"]) == 0
    assert json.loads(capsys.readouterr().out)["events"][0]["action"] == "release.publish"


def test_cohort_list_and_assign(wired, tmp_path, capsys):
    import json
    store, _ = wired
    store.upsert_device(device_id="d1", product_id=BID)
    assert main(["client", "cohort", "assign", "--cohort", "beta", "--device-id", "d1"]) == 0
    assert "assigned 1/1 device(s) to cohort beta" in capsys.readouterr().out
    assert main(["client", "cohort", "list"]) == 0
    assert json.loads(capsys.readouterr().out) == {"cohorts": [
        {"cohort": "beta", "devices": 1, "by_product": {str(BID): 1}}]}


def test_cohort_assign_whole_product(wired, tmp_path, capsys):
    """The bulk selector: --product-id moves every device of the product in one call --
    the surgical per-id path stays for anything finer."""
    store, _ = wired
    for d in ("d1", "d2", "d3"):
        store.upsert_device(device_id=d, product_id=BID)
    store.upsert_device(device_id="other", product_id=BID + 1)
    assert main(["client", "cohort", "assign", "--cohort", "beta",
                 "--product-id", str(BID)]) == 0
    assert "assigned 3 device(s) (product %d) to cohort beta" % BID in capsys.readouterr().out
    assert store.get_device("other")["cohort"] == "__default__"   # untouched


def test_cohort_assign_requires_exactly_one_selector(wired, tmp_path, capsys):
    """--id and --product-id are mutually exclusive at the prompt, and the API enforces
    the same rule for direct callers."""
    import pytest

    from openmv_ota.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["client", "cohort", "assign", "--cohort", "b",
                                   "--device-id", "d1", "--product-id", "7"])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["client", "cohort", "assign", "--cohort", "b"])


def test_cohort_error_surfaced(tmp_path, monkeypatch, capsys):
    app, store = _server(tmp_path, scopes=("observe",))     # token can't control -> assign 403s
    tc = TestClient(app)
    monkeypatch.setattr(client_cli, "_make_api", lambda cfg: Api(cfg, client=tc))
    monkeypatch.setenv("OPENMV_OTA_SERVER", "https://ota.test")
    monkeypatch.setenv("OPENMV_OTA_TOKEN", "tok")
    assert main(["client", "cohort", "assign", "--cohort", "b", "--device-id", "d1"]) == 1
    assert "403" in capsys.readouterr().err


def test_pin_device_and_cohort(wired, tmp_path, capsys):
    store, _ = wired
    store.upsert_device(device_id="d1", product_id=BID)
    assert main(["client", "device", "pin", "--device-id", "d1", "--release", "rel9"]) == 0
    assert "device d1 pinned to rel9" in capsys.readouterr().out
    assert store.get_device("d1")["pinned_release_id"] == "rel9"
    assert main(["client", "device", "pin", "--device-id", "d1", "--clear"]) == 0
    assert "(unpinned)" in capsys.readouterr().out
    assert main(["client", "cohort", "pin", "--product-id", str(BID),
                 "--cohort", "beta", "--release", "rel9"]) == 0
    assert "cohort beta pinned to rel9" in capsys.readouterr().out
    assert store.get_cohort_pin(BID, "beta") == "rel9"


def test_bind_device(wired, tmp_path, capsys):
    store, _ = wired
    store.upsert_device(device_id="d1", product_id=BID)
    assert main(["client", "device", "bind", "--device-id", "d1"]) == 0
    # the wired token acts for the implicit '' account -> renders as (unassigned)
    assert "device d1 bound to (unassigned)" in capsys.readouterr().out
    assert store.device_account("d1")["source"] == "admin"


def _wire_super_admin(tmp_path, monkeypatch, scopes):
    app, store = _server(tmp_path, scopes=scopes)
    tc = TestClient(app)
    monkeypatch.setattr(client_cli, "_make_api", lambda cfg: Api(cfg, client=tc))
    monkeypatch.setenv("OPENMV_OTA_SERVER", "https://ota.test")
    monkeypatch.setenv("OPENMV_OTA_TOKEN", "tok")
    return store


def test_account_create_and_list(tmp_path, monkeypatch, capsys):
    _wire_super_admin(tmp_path, monkeypatch, scopes=("accounts",))
    assert main(["client", "account", "create", "--name", "DroneCo"]) == 0
    out = capsys.readouterr().out
    assert "created" in out and "working token" in out
    assert main(["client", "account", "list"]) == 0
    assert "DroneCo" in capsys.readouterr().out


def test_account_error_surfaced(tmp_path, monkeypatch, capsys):
    _wire_super_admin(tmp_path, monkeypatch, scopes=("observe",))    # no accounts -> 403
    assert main(["client", "account", "create", "--name", "X"]) == 1
    assert "403" in capsys.readouterr().err


def test_account_lifecycle_verbs(tmp_path, monkeypatch, capsys):
    store = _wire_super_admin(tmp_path, monkeypatch, scopes=("accounts",))
    store.add_account("acctA", "A")
    store.add_token(hash_token("x"), "t", ["observe"], account_id="acctA")   # a token to revoke
    assert main(["client", "account", "rename", "--account-id", "acctA", "--name", "New"]) == 0
    assert "renamed to New" in capsys.readouterr().out
    assert main(["client", "account", "deactivate", "--account-id", "acctA"]) == 0
    assert "deactivated" in capsys.readouterr().out and store.get_account("acctA")["active"] == 0
    assert main(["client", "account", "activate", "--account-id", "acctA"]) == 0
    assert "activated" in capsys.readouterr().out


def test_token_verbs(tmp_path, monkeypatch, capsys):
    store = _wire_super_admin(tmp_path, monkeypatch, scopes=("accounts",))
    store.add_account("acctA", "A")
    assert main(["client", "token", "issue", "--account-id", "acctA", "--name", "ci"]) == 0
    assert "issued for acctA" in capsys.readouterr().out
    th = store.list_tokens(account_id="acctA")[0]["token_hash"]
    assert main(["client", "token", "list", "--account-id", "acctA"]) == 0
    assert "acctA" in capsys.readouterr().out
    assert main(["client", "token", "rotate", th]) == 0
    assert "rotated ->" in capsys.readouterr().out
    live = [t for t in store.list_tokens(account_id="acctA") if not t["revoked"]][0]["token_hash"]
    assert main(["client", "token", "revoke", live]) == 0
    assert "revoked" in capsys.readouterr().out


def test_token_verb_error_surfaced(tmp_path, monkeypatch, capsys):
    _wire_super_admin(tmp_path, monkeypatch, scopes=("manage",))    # no accounts scope -> 403
    assert main(["client", "token", "issue", "--account-id", "acctA", "--name", "x"]) == 1
    assert "403" in capsys.readouterr().err


def test_bind_error_surfaced(tmp_path, monkeypatch, capsys):
    app, store = _server(tmp_path, scopes=("observe",))     # token can't control -> bind 403s
    tc = TestClient(app)
    monkeypatch.setattr(client_cli, "_make_api", lambda cfg: Api(cfg, client=tc))
    monkeypatch.setenv("OPENMV_OTA_SERVER", "https://ota.test")
    monkeypatch.setenv("OPENMV_OTA_TOKEN", "tok")
    assert main(["client", "device", "bind", "--device-id", "d1"]) == 1
    assert "403" in capsys.readouterr().err


def test_pin_error_surfaced(tmp_path, monkeypatch, capsys):
    app, store = _server(tmp_path, scopes=("observe",))     # token can't control -> pin 403s
    store.upsert_device(device_id="d1", product_id=BID)
    tc = TestClient(app)
    monkeypatch.setattr(client_cli, "_make_api", lambda cfg: Api(cfg, client=tc))
    monkeypatch.setenv("OPENMV_OTA_SERVER", "https://ota.test")
    monkeypatch.setenv("OPENMV_OTA_TOKEN", "tok")
    assert main(["client", "device", "pin", "--device-id", "d1", "--release", "r"]) == 1
    assert "403" in capsys.readouterr().err


def test_missing_creds(tmp_path, monkeypatch, capsys):
    # The server URL always resolves (the hosted default backstops it) -- the token is the
    # only credential with no default, so it is what a bare verb fails on.
    monkeypatch.delenv("OPENMV_OTA_SERVER", raising=False)
    monkeypatch.delenv("OPENMV_OTA_TOKEN", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))       # no saved profile
    assert main(["client", "fleet"]) == 2
    assert "no API token" in capsys.readouterr().err


def test_client_no_subcommand(capsys):
    assert main(["client"]) == 1


# --- --json on the write verbs ------------------------------------------------------------------

def _ns(argv):
    from openmv_ota.cli import build_parser
    return build_parser().parse_args(argv)


def test_every_client_verb_accepts_json(capsys):
    """Reads have always printed JSON; the writes printed prose, so publishing a release or
    issuing a token could not be scripted without parsing English. A group where one verb speaks
    JSON and its neighbour does not is the inconsistency this removes, so `--json` is on ALL of
    them -- including the local `login`/`logout`, which write the saved profile rather than call
    the API."""
    import re
    import subprocess
    import sys

    def h(a):
        r = subprocess.run([sys.executable, "-m", "openmv_ota.cli"] + a + ["--help"],
                           capture_output=True, text=True)
        return (r.stdout or "") + (r.stderr or "")

    def subs(t):
        m = re.search(r"\{([a-z,_]+)\}", t)
        return m.group(1).split(",") if m else []

    cmds = []
    for s in subs(h(["client"])):
        third = subs(h(["client", s]))
        cmds += [["client", s, t] for t in third] if third else [["client", s]]
    assert cmds, "no client verbs discovered"
    assert [" ".join(c) for c in cmds if "--json" not in h(c)] == []


def test_write_verbs_emit_the_servers_own_response(monkeypatch, capsys):
    """`--json` prints the response VERBATIM, not a re-rendering -- so a field the prose summary
    does not mention is still there for a caller that needs it. The account secret is the case
    that matters: it exists for exactly one response, and a script that cannot capture it has to
    mint another account."""
    import json
    import types

    from openmv_ota.client import cli as ccli

    class FakeApi:
        def create_account(self, name):
            return {"account_id": "acct_1", "name": name, "token": "SECRET", "extra": 1}

    monkeypatch.setattr(ccli, "_make_api", lambda cfg: FakeApi())
    monkeypatch.setattr(ccli.config, "resolve",
                        lambda s, t: types.SimpleNamespace(server_url="x", token="y"))
    ns = _ns(["client", "account", "create", "--name", "DroneCo", "--json"])
    assert ns.func(ns) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["token"] == "SECRET", "the one-time secret must survive into the JSON"
    assert body["extra"] == 1, "verbatim: a field the summary ignores is still delivered"


def test_write_verbs_still_print_prose_without_json(monkeypatch, capsys):
    import types

    from openmv_ota.client import cli as ccli

    class FakeApi:
        def assign_cohort(self, cohort, device_ids=None, product_id=None):
            devices = device_ids
            return {"cohort": cohort, "assigned": len(devices)}

    monkeypatch.setattr(ccli, "_make_api", lambda cfg: FakeApi())
    monkeypatch.setattr(ccli.config, "resolve",
                        lambda s, t: types.SimpleNamespace(server_url="x", token="y"))
    ns = _ns(["client", "cohort", "assign", "--cohort", "beta", "--device-id", "d1"])
    assert ns.func(ns) == 0
    assert "assigned 1/1 device(s) to cohort beta" in capsys.readouterr().out


def test_publish_attaches_a_rendered_sbom(wired, tmp_path, capsys, monkeypatch):
    """publish renders the SBOM fresh from the committed lock and ships it with the release --
    dependency evidence beside the bytes it describes."""
    import openmv_ota.build.sbom as sbom_mod
    store, _tmp = wired
    project = tmp_path / "proj"
    _build_release(project)
    monkeypatch.setattr(sbom_mod, "render_sbom", lambda p: '{"bomFormat": "CycloneDX"}')
    assert main(["client", "release", "publish", str(project), "-b", "OPENMV_N6"]) == 0
    rel = store.list_releases(BID)[0]
    assert rel["sbom_key"] == "sbom/%s/sbom.cdx.json" % rel["release_id"]


def test_publish_warns_but_ships_without_an_sbom(wired, tmp_path, capsys):
    """A project the renderer cannot read publishes WITHOUT an SBOM, with a warning --
    evidence is worth carrying, never worth blocking a release over. (The fake project here
    has no lock, so the real renderer fails.)"""
    store, _ = wired
    project = tmp_path / "proj"
    _build_release(project)
    assert main(["client", "release", "publish", str(project), "-b", "OPENMV_N6"]) == 0
    assert "no SBOM attached" in capsys.readouterr().err
    assert store.list_releases(BID)[0]["sbom_key"] is None


def test_rollout_create_status_and_list(wired, tmp_path, capsys):
    """A rollout can be staged AFTER publish (`rollout create`), found again
    (`client rollouts`), and its counters read (`rollout status`) -- losing the publish
    output no longer strands the operator with an id nothing can recover."""
    import json

    store, root = wired
    _build_release(root / "p")
    assert main(["client", "release", "publish", str(root / "p"), "-b", "OPENMV_N6", "--json"]) == 0
    rel = json.loads(capsys.readouterr().out)["release_id"]

    assert main(["client", "rollout", "create", "--release-id", rel, "--cohort", "beta",
                 "--percent", "5"]) == 0
    out = capsys.readouterr().out
    assert "cohort=beta" in out and "5.0%" in out

    assert main(["client", "rollout", "list", "--product-id", str(BID), "--limit", "10",
                 "--offset", "0", "--state", "active"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["total"] == 1 and body["rollouts"][0]["release_id"] == rel
    # the list is the INVENTORY: identity/state/dial/audience -- the counters are status's
    row = body["rollouts"][0]
    assert "attempted" not in row and "cohort_devices" in row
    rid = row["rollout_id"]
    assert main(["client", "rollout", "list", "--state", "stopped"]) == 0
    assert json.loads(capsys.readouterr().out)["rollouts"] == []

    assert main(["client", "rollout", "status", "--rollout-id", rid]) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["state"] == "active" and st["attempted"] == 0


def test_cohort_rename_relabels_everything_at_once(wired, tmp_path, capsys):
    """Rename touches the three places a cohort name lives -- device rows, rollouts,
    pins -- in one commit, so a mid-flight rollout keeps its audience under the new name."""
    import json

    store, root = wired
    _build_release(root / "p")
    assert main(["client", "release", "publish", str(root / "p"), "-b", "OPENMV_N6", "--json"]) == 0
    rel = json.loads(capsys.readouterr().out)["release_id"]
    for d in ("d1", "d2"):
        store.upsert_device(device_id=d, product_id=BID, cohort="beta")
    assert main(["client", "rollout", "create", "--release-id", rel, "--cohort", "beta",
                 "--percent", "5"]) == 0
    assert main(["client", "cohort", "pin", "--product-id", str(BID), "--cohort", "beta",
                 "--release-id", rel]) == 0
    capsys.readouterr()

    assert main(["client", "cohort", "rename", "--cohort", "beta", "--name", "pilot"]) == 0
    assert "beta renamed to pilot (2 device(s), 1 rollout(s), 1 pin(s))" \
        in capsys.readouterr().out
    assert store.get_device("d1")["cohort"] == "pilot"
    assert store.list_rollouts(BID)[0]["cohort"] == "pilot"
    assert store.get_cohort_pin(BID, "pilot") == rel and store.get_cohort_pin(BID, "beta") is None

    # renaming ONTO a name in use is refused -- merging is `assign`, never a rename surprise
    store.upsert_device(device_id="d3", product_id=BID, cohort="bench")
    assert main(["client", "cohort", "rename", "--cohort", "bench", "--name", "pilot"]) == 1
    assert "already in use" in capsys.readouterr().err


def test_cohort_delete_returns_devices_to_default(wired, tmp_path, capsys):
    """Delete retires the label: devices back to __default__, pins dropped -- but never
    out from under an ACTIVE rollout (pause or stop it first)."""
    import json

    store, root = wired
    _build_release(root / "p")
    assert main(["client", "release", "publish", str(root / "p"), "-b", "OPENMV_N6", "--json"]) == 0
    rel = json.loads(capsys.readouterr().out)["release_id"]
    store.upsert_device(device_id="d1", product_id=BID, cohort="beta")
    assert main(["client", "rollout", "create", "--release-id", rel, "--cohort", "beta",
                 "--percent", "5", "--json"]) == 0
    rid = json.loads(capsys.readouterr().out)["rollout_id"]
    assert main(["client", "cohort", "pin", "--product-id", str(BID), "--cohort", "beta",
                 "--release-id", rel]) == 0
    capsys.readouterr()

    assert main(["client", "cohort", "delete", "--cohort", "beta"]) == 1   # active rollout
    assert "active rollout" in capsys.readouterr().err
    assert main(["client", "rollout", "pause", "--rollout-id", rid]) == 0
    capsys.readouterr()
    assert main(["client", "cohort", "delete", "--cohort", "beta"]) == 0
    assert "1 device(s) back to __default__, 1 pin(s) dropped" in capsys.readouterr().out
    assert store.get_device("d1")["cohort"] == "__default__"
    assert store.get_cohort_pin(BID, "beta") is None


def test_release_show_and_device_show(wired, tmp_path, capsys):
    """The single-entity reads: the CRUD 'R' the list verbs alone couldn't give a script
    without paging until the row turned up."""
    import json

    store, root = wired
    _build_release(root / "p")
    assert main(["client", "release", "publish", str(root / "p"), "-b", "OPENMV_N6",
                 "--json"]) == 0
    rel = json.loads(capsys.readouterr().out)["release_id"]
    store.upsert_device(device_id="d1", product_id=BID)

    assert main(["client", "release", "show", "--release-id", rel]) == 0
    assert json.loads(capsys.readouterr().out)["release_id"] == rel
    assert main(["client", "device", "show", "--device-id", "d1"]) == 0
    assert json.loads(capsys.readouterr().out)["device_id"] == "d1"


def test_release_sbom_download(wired, tmp_path, capsys):
    """`release sbom` hands back exactly the uploaded evidence -- to stdout for piping
    into a scanner, or to a file with -o. A release without one is a clean error."""
    import json

    store, root = wired
    _build_release(root / "p")
    assert main(["client", "release", "publish", str(root / "p"), "-b", "OPENMV_N6",
                 "--json"]) == 0
    rel = json.loads(capsys.readouterr().out)["release_id"]
    assert main(["client", "release", "sbom", "--release-id", rel]) == 1   # none attached
    assert "404" in capsys.readouterr().err

    sbom = json.dumps({"bomFormat": "CycloneDX", "components": []}).encode()
    key = "sbom/%s/sbom.cdx.json" % rel
    # attach one the way publish would have (the fixture project has no lock to render)
    from openmv_ota.server.storage import LocalArtifactStorage
    # reach the wired server's storage through the metastore row + a fresh handle
    row = store.get_release(rel)
    assert row["sbom_key"] is None
    store.execute("UPDATE releases SET sbom_key = ? WHERE release_id = ?", (key, rel))
    LocalArtifactStorage(str(tmp_path / "blobs")).put(key, sbom, "application/json")

    assert main(["client", "release", "sbom", "--release-id", rel]) == 0
    assert json.loads(capsys.readouterr().out)["bomFormat"] == "CycloneDX"
    out = tmp_path / "sbom.json"
    assert main(["client", "release", "sbom", "--release-id", rel, "-o", str(out)]) == 0
    assert "saved" in capsys.readouterr().out and out.read_bytes() == sbom


def _build_release_with_trailer(project, pv=0x02000000):
    """Like ``_build_release`` but the image is SLOT-SHAPED -- body, 0xFF pad, and a real
    trailer in the final control block -- so `release bases --fleet` can verify the stored
    bytes against the fleet's reported body sha. Returns the trailer's body sha (hex)."""
    from openmv_ota.ota.trailer import Trailer, pack_trailer

    build = project / "build"
    build.mkdir(parents=True, exist_ok=True)
    body_bytes = b"\xA5" * 64
    body_sha = hashlib.sha256(body_bytes).digest()
    packed = pack_trailer(Trailer(
        body_size=len(body_bytes), pad_size=0, meta={}, product_id=BID,
        min_platform_version=0, payload_version=pv, reserved0=0,
        key_id=0x0100, sig_alg=ES256, body_sha256=body_sha, signature=b"\x00" * 64))
    # the trailer FRONT-ANCHORS the final 4 KiB control block (parse_trailer reads the
    # header first), exactly as _compose_slot lays a real slot out
    img = (body_bytes + b"\xff" * (8192 - len(body_bytes) - 4096)
           + packed + b"\xff" * (4096 - len(packed)))
    image_gz = gzip.compress(img, mtime=0)
    body = {"schema": 1, "product_id": BID, "product": "P", "version": "2.0.0",
            "payload_version": pv, "min_platform_version": 0, "size": len(img),
            "sha256": hashlib.sha256(img).hexdigest(),
            "representations": [{"format": "full", "url": "OPENMV_N6-ota.img.gz",
                                 "size": len(image_gz)}]}
    manifest = pack_manifest(Manifest(body=body, key_id=0x0100, sig_alg=ES256,
                                      signature=b"\x00" * 64))
    (build / "OPENMV_N6-manifest.bin").write_bytes(manifest)
    (build / "OPENMV_N6-ota.img.gz").write_bytes(image_gz)
    return body_sha.hex()


def test_release_bases_fleet_covers_what_the_fleet_runs(wired, tmp_path, capsys):
    """--fleet asks the server's fleet report and downloads one base per (version, bytes)
    group it can cover, warning AT FETCH TIME about the two groups it can't: a pruned
    release, and a republish whose stored bytes differ from what devices run."""
    import json

    store, root = wired
    sha = _build_release_with_trailer(root / "p")
    assert main(["client", "release", "publish", str(root / "p"), "-b", "OPENMV_N6",
                 "--json"]) == 0
    json.loads(capsys.readouterr().out)
    pv = 0x02000000
    store.upsert_device(device_id="ok1", product_id=BID,
                        current_payload_version=pv, body_sha256=sha)
    store.upsert_device(device_id="ok2", product_id=BID,
                        current_payload_version=pv, body_sha256=sha)
    store.upsert_device(device_id="split", product_id=BID,
                        current_payload_version=pv, body_sha256="ff" * 32)   # republish split
    store.upsert_device(device_id="pruned", product_id=BID,
                        current_payload_version=0x01000000, body_sha256="aa" * 32)
    store.upsert_device(device_id="shaless", product_id=BID,
                        current_payload_version=pv)                          # full by design
    # ...and a release whose STORED image has no readable trailer at all: unverifiable
    # bytes can't cover anyone, so it warns like a republish split
    _build_release(root / "p2", pv=0x03000000)
    assert main(["client", "release", "publish", str(root / "p2"), "-b", "OPENMV_N6"]) == 0
    capsys.readouterr()
    store.upsert_device(device_id="unverif", product_id=BID,
                        current_payload_version=0x03000000, body_sha256="dd" * 32)

    out = tmp_path / "bases"
    assert main(["client", "release", "bases", "--fleet", "-b", "OPENMV_N6",
                 "--product-id", str(BID), "-o", str(out)]) == 0
    text, err = capsys.readouterr()
    assert "2 device(s)" in text                       # the covered group, with its count
    assert (out / "OPENMV_N6-base-2.0.0.img.gz").read_bytes() == (
        root / "p" / "build" / "OPENMV_N6-ota.img.gz").read_bytes()
    assert "no stored release matches" in err
    assert err.count("different bytes than the store") == 2   # the split AND the unverifiable


def test_release_bases_fleet_needs_a_product_and_says_when_nothing_covers(wired, tmp_path,
                                                                          capsys):
    store, root = wired
    assert main(["client", "release", "bases", "--fleet", "-b", "OPENMV_N6",
                 "-o", str(tmp_path / "b")]) == 2
    assert "needs --product-id" in capsys.readouterr().err
    assert main(["client", "release", "bases", "--fleet", "-b", "OPENMV_N6",
                 "--product-id", str(BID), "-o", str(tmp_path / "b")]) == 0
    assert "no coverable fleet bases" in capsys.readouterr().out


def test_device_rename_and_clear(wired, tmp_path, capsys):
    store, _ = wired
    store.upsert_device(device_id="d1", product_id=BID)
    assert main(["client", "device", "rename", "--device-id", "d1",
                 "--name", "Loading dock east"]) == 0
    assert "named 'Loading dock east'" in capsys.readouterr().out
    assert store.get_device("d1")["display_name"] == "Loading dock east"
    assert main(["client", "device", "rename", "--device-id", "d1", "--clear"]) == 0
    assert "name cleared" in capsys.readouterr().out
    assert store.get_device("d1")["display_name"] == ""
    # audited like every mutation
    assert any(e["action"] == "device.rename" for e in store.read_audit())


def _fake_osv_hits(monkeypatch, table):
    """Findings for (name, version) pairs in ``table``; everything else clean."""
    from openmv_ota.server.advisor import OsvClient

    def scan(self, components):
        out = []
        for c in components:
            for f in table.get((c.get("name"), c.get("version")), []):
                out.append(dict(f, component=c["name"], version=c["version"]))
        return out

    monkeypatch.setattr(OsvClient, "scan", scan)


def _seed_scannable(store, storage, *, with_device=True):
    """A release with an SBOM in storage, and (optionally) a device running it --
    the scanner's whole scope condition."""
    sbom = {"components": [{"name": "mbedtls", "version": "3.5.1"},
                           {"name": "lwip", "version": "2.2.0"}]}
    storage.put("sbom/rel1/sbom.cdx.json", json.dumps(sbom).encode(), "application/json")
    store.add_release(release_id="rel1", product_id=BID, product="P", version="2.0.0",
                      payload_version=0x02000000, min_platform_version=0,
                      image_sha256="ab" * 32, image_size=1,
                      representations=[{"format": "full", "url": "x", "size": 1}],
                      manifest_key="m", image_key="i",
                      sbom_key="sbom/rel1/sbom.cdx.json")
    if with_device:
        store.upsert_device(device_id="d1", product_id=BID, current_version="2.0.0")


MBEDTLS_CVE = {("mbedtls", "3.5.1"): [
    {"vuln_id": "CVE-2026-21437", "severity": "high", "summary": "x509 overflow"}]}


def test_advisories_scan_finds_clears_and_lists(wired, tmp_path, monkeypatch, capsys):
    store, _ = wired
    storage = LocalArtifactStorage(str(tmp_path / "blobs"))
    _seed_scannable(store, storage)
    _fake_osv_hits(monkeypatch, MBEDTLS_CVE)
    assert main(["client", "advisories", "scan"]) == 0
    out = capsys.readouterr().out
    assert "scanned 1 release(s): 1 finding(s), 1 new" in out
    assert "NEW CVE-2026-21437  high  mbedtls 3.5.1" in out
    # listed as active, with the full row
    assert main(["client", "advisories", "list"]) == 0
    rows = json.loads(capsys.readouterr().out)["advisories"]
    assert rows[0]["vuln_id"] == "CVE-2026-21437" and rows[0]["cleared_at"] is None
    # a second identical scan is not "new" again -- the notification edge
    assert main(["client", "advisories", "scan"]) == 0
    assert "1 finding(s), 0 new" in capsys.readouterr().out
    # the vulnerability database moves on: the finding clears, history remains
    _fake_osv_hits(monkeypatch, {})
    assert main(["client", "advisories", "scan"]) == 0
    capsys.readouterr()
    assert main(["client", "advisories", "list"]) == 0
    assert json.loads(capsys.readouterr().out)["advisories"] == []
    assert main(["client", "advisories", "list", "--all"]) == 0
    rows = json.loads(capsys.readouterr().out)["advisories"]
    assert rows[0]["cleared_at"] is not None
    # audited like every mutation
    assert any(e["action"] == "advisory.scan" for e in store.read_audit())


def test_advisories_scan_scope(wired, tmp_path, monkeypatch, capsys):
    store, _ = wired
    storage = LocalArtifactStorage(str(tmp_path / "blobs"))
    # no device runs it and no rollout offers it -> not scanned
    _seed_scannable(store, storage, with_device=False)
    _fake_osv_hits(monkeypatch, MBEDTLS_CVE)
    assert main(["client", "advisories", "scan"]) == 0
    assert "scanned 0 release(s)" in capsys.readouterr().out
    # an ACTIVE rollout pulls it back into scope even with zero devices on it
    store.add_rollout(rollout_id="ro1", release_id="rel1", product_id=BID,
                      cohort="__default__", percent=10)
    assert main(["client", "advisories", "scan"]) == 0
    assert "scanned 1 release(s): 1 finding(s), 1 new" in capsys.readouterr().out
    # single-release scan + the 404 edge
    assert main(["client", "advisories", "scan", "--release-id", "rel1"]) == 0
    assert "scanned 1 release(s)" in capsys.readouterr().out
    assert main(["client", "advisories", "scan", "--release-id", "rel_ghost"]) == 1
    capsys.readouterr()
    assert main(["client", "advisories", "list", "--release-id", "rel_ghost"]) == 1
    capsys.readouterr()


def test_publish_reports_advisories(wired, tmp_path, monkeypatch, capsys):
    store, _ = wired
    project = tmp_path / "proj"
    _build_release(project)
    import openmv_ota.build.sbom as sbom_mod
    monkeypatch.setattr(sbom_mod, "render_sbom", lambda proj: json.dumps(
        {"components": [{"name": "mbedtls", "version": "3.5.1"}]}))
    _fake_osv_hits(monkeypatch, MBEDTLS_CVE)
    assert main(["client", "release", "publish", str(project), "-b", "OPENMV_N6"]) == 0
    out = capsys.readouterr().out
    assert "published rel_" in out
    # the maker walks away knowing what the new release carries
    assert "advisory: CVE-2026-21437  high  mbedtls 3.5.1" in out
    # a clean SBOM says so explicitly
    _fake_osv_hits(monkeypatch, {})
    _build_release(project, pv=0x02000100)
    assert main(["client", "release", "publish", str(project), "-b", "OPENMV_N6"]) == 0
    assert "no known vulnerabilities" in capsys.readouterr().out


def test_publish_advisory_scan_unavailable(wired, tmp_path, monkeypatch, capsys):
    store, _ = wired
    project = tmp_path / "proj"
    _build_release(project)
    import openmv_ota.build.sbom as sbom_mod
    monkeypatch.setattr(sbom_mod, "render_sbom", lambda proj: json.dumps(
        {"components": [{"name": "x", "version": "1"}]}))
    from openmv_ota.client.api import Api, ClientError

    def boom(self, release_id=None):
        raise ClientError("scan endpoint down", exit_code=1)
    monkeypatch.setattr(Api, "scan_advisories", boom)
    assert main(["client", "release", "publish", str(project), "-b", "OPENMV_N6"]) == 0
    out = capsys.readouterr().out
    assert "published rel_" in out and "advisory scan unavailable" in out


def test_publish_survives_scanner_crash(wired, tmp_path, monkeypatch, capsys):
    # The publish-time background scan blowing up must never touch the publish.
    store, _ = wired
    project = tmp_path / "proj"
    _build_release(project)
    import openmv_ota.build.sbom as sbom_mod
    monkeypatch.setattr(sbom_mod, "render_sbom", lambda proj: json.dumps(
        {"components": [{"name": "x", "version": "1"}]}))
    from openmv_ota.server.advisor import OsvClient
    calls = {"n": 0}

    def crash_once(self, components):
        calls["n"] += 1
        if calls["n"] == 1:                     # the publish-time background scan
            raise RuntimeError("osv exploded")
        return []
    monkeypatch.setattr(OsvClient, "scan", crash_once)
    assert main(["client", "release", "publish", str(project), "-b", "OPENMV_N6"]) == 0
    out = capsys.readouterr()
    assert "published rel_" in out.out
    assert "publish-time advisory scan failed" in out.err


def test_release_artifact_download(wired, tmp_path, capsys):
    store, _ = wired
    storage = LocalArtifactStorage(str(tmp_path / "blobs"))   # same root the server uses
    store.add_release(release_id="rel1", product_id=BID, product="P", version="2.0.0",
                      payload_version=0x02000000, min_platform_version=0, image_sha256="ab" * 32,
                      image_size=4, manifest_key="m", image_key="artifacts/rel1/img.gz",
                      representations=[
                          {"format": "full", "url": "img.gz", "size": 4},
                          {"format": "ocdl", "url": "img.delta-1.9.0.gz", "size": 2,
                           "base_payload_version": 0x01090000, "base_body_sha256": "cd" * 32}])
    storage.put("artifacts/rel1/img.gz", b"FULL", "application/gzip")
    storage.put("artifacts/rel1/img.delta-1.9.0.gz", b"DP", "application/gzip")
    assert main(["client", "release", "artifact", "--release-id", "rel1",
                 "--filename", "img.delta-1.9.0.gz",
                 "-o", str(tmp_path / "d.gz")]) == 0
    assert "saved" in capsys.readouterr().out
    assert (tmp_path / "d.gz").read_bytes() == b"DP"
    # default output name = the artifact filename (written into the cwd)
    import os
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        assert main(["client", "release", "artifact", "--release-id", "rel1",
                     "--filename", "img.gz"]) == 0
    finally:
        os.chdir(cwd)
    assert (tmp_path / "img.gz").read_bytes() == b"FULL"
    capsys.readouterr()
    # a filename the manifest never declared is a 404 (whitelist, not a path)
    assert main(["client", "release", "artifact", "--release-id", "rel1",
                 "--filename", "evil.gz", "-o", str(tmp_path / "x")]) == 1
    capsys.readouterr()
    # declared but no longer retained
    storage.delete("artifacts/rel1/img.gz")
    assert main(["client", "release", "artifact", "--release-id", "rel1",
                 "--filename", "img.gz", "-o", str(tmp_path / "y")]) == 1
    assert "404" in capsys.readouterr().err


def test_release_manifest_download(wired, tmp_path, capsys):
    store, _ = wired
    storage = LocalArtifactStorage(str(tmp_path / "blobs"))
    store.add_release(release_id="rel1", product_id=BID, product="P", version="2.0.0",
                      payload_version=0x02000000, min_platform_version=0,
                      image_sha256="ab" * 32, image_size=1, representations=[],
                      manifest_key="manifests/rel1/manifest.bin", image_key="i")
    storage.put("manifests/rel1/manifest.bin", b"SIGNEDMANIFEST", "application/octet-stream")
    assert main(["client", "release", "manifest", "--release-id", "rel1",
                 "-o", str(tmp_path / "m.bin")]) == 0
    assert "saved" in capsys.readouterr().out
    assert (tmp_path / "m.bin").read_bytes() == b"SIGNEDMANIFEST"
    storage.delete("manifests/rel1/manifest.bin")
    assert main(["client", "release", "manifest", "--release-id", "rel1",
                 "-o", str(tmp_path / "n.bin")]) == 1               # retained no longer
    assert "404" in capsys.readouterr().err


def test_release_and_rollout_rename(wired, capsys):
    store, _ = wired
    store.add_release(release_id="rel1", product_id=BID, product="P", version="2.0.0",
                      payload_version=0x02000000, min_platform_version=0, image_sha256="ab" * 32,
                      image_size=1, representations=[{"format": "full", "url": "x", "size": 1}],
                      manifest_key="m", image_key="i")
    rid = "rel1"
    # a label, never identity: need not be unique, renamable at any time
    assert main(["client", "release", "rename", "--release-id", rid,
                 "--name", "Night vision tuning"]) == 0
    assert "named 'Night vision tuning'" in capsys.readouterr().out
    assert store.get_release(rid)["display_name"] == "Night vision tuning"
    assert main(["client", "rollout", "create", "--release-id", rid,
                 "--percent", "10", "--name", "Beta wave"]) == 0
    ro = store.list_rollouts()[0]
    assert ro["display_name"] == "Beta wave"
    capsys.readouterr()
    assert main(["client", "rollout", "list"]) == 0             # the API row carries the label
    assert json.loads(capsys.readouterr().out)["rollouts"][0]["display_name"] == "Beta wave"
    capsys.readouterr()
    assert main(["client", "rollout", "rename", "--rollout-id", ro["rollout_id"],
                 "--name", "Beta wave 2"]) == 0
    assert "named 'Beta wave 2'" in capsys.readouterr().out
    assert main(["client", "rollout", "rename", "--rollout-id", ro["rollout_id"],
                 "--clear"]) == 0
    assert "name cleared" in capsys.readouterr().out
    assert store.get_rollout(ro["rollout_id"])["display_name"] == ""
    assert main(["client", "release", "rename", "--release-id", rid, "--clear"]) == 0
    assert store.get_release(rid)["display_name"] == ""
    # audited like every mutation
    actions = [e["action"] for e in store.read_audit()]
    assert "release.rename" in actions and "rollout.rename" in actions


def test_release_rollout_rename_errors(wired, capsys):
    store, _ = wired
    assert main(["client", "release", "rename", "--release-id", "rel_ghost",
                 "--name", "x"]) == 1                              # 404 -> exit 1
    capsys.readouterr()
    assert main(["client", "rollout", "rename", "--rollout-id", "ro_ghost",
                 "--name", "x"]) == 1
    capsys.readouterr()


def test_device_rename_errors(wired, tmp_path, capsys):
    store, _ = wired
    assert main(["client", "device", "rename", "--device-id", "ghost",
                 "--name", "x"]) == 1                              # 404 -> exit 1
    capsys.readouterr()
    store.upsert_device(device_id="d1", product_id=BID)
    assert main(["client", "device", "rename", "--device-id", "d1",
                 "--name", "x" * 65]) == 1                         # 400 too long
    assert "400" in capsys.readouterr().err

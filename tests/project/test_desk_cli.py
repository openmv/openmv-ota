"""`openmv-ota project keys desk ...`: make, list, renew, revoke and push desk keys."""

from __future__ import annotations

from openmv_ota.cli import main
from openmv_ota.project import desk_keys
from openmv_ota.project.passphrase import dev_passphrase_path


def _root(tmp_path):
    p = dev_passphrase_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("dev-pass", encoding="utf-8")
    return tmp_path


def test_create_list_renew_revoke(tmp_path, capsys):
    root = str(_root(tmp_path))
    assert main(["project", "keys", "desk", "list", root]) == 0
    assert "No desk keys" in capsys.readouterr().out
    assert main(["project", "keys", "desk", "create", "--days", "30", "bench-1", root]) == 0
    out = capsys.readouterr().out
    kid = str(desk_keys.read(root)[0].key_id)
    assert ("Desk key %s for 'bench-1'" % kid) in out and "desk push" in out
    assert main(["project", "keys", "desk", "create", "bench-1", root]) != 0      # one per desk
    assert "already has a key" in capsys.readouterr().err
    assert main(["project", "keys", "desk", "list", root]) == 0
    assert "bench-1" in capsys.readouterr().out
    assert main(["project", "keys", "desk", "renew", kid, root]) == 0
    assert "now expires" in capsys.readouterr().out
    assert main(["project", "keys", "desk", "revoke", kid, root]) == 0
    assert ("Revoked desk key %s" % kid) in capsys.readouterr().out
    assert main(["project", "keys", "desk", "list", root]) == 0
    assert "revoked" in capsys.readouterr().out
    assert main(["project", "keys", "desk", "renew", kid, root]) != 0             # revoked
    assert main(["project", "keys", "desk", "revoke", "7", root]) != 0
    from openmv_ota.project import history
    actions = [e["action"] for e in history.read(root)]
    assert actions == ["desk-key-create", "desk-key-renew", "desk-key-revoke"]


def test_push_sends_every_certified_desk_key(tmp_path, capsys, monkeypatch):
    from openmv_ota.project import issuer
    from tests.project.test_issuer import roots_for
    root = _root(tmp_path)
    roots_for(root, "dev-pass")
    issuer.create(root, "dev-pass")
    bench = desk_keys.create(root, "bench-1", "dev-pass")
    factory = desk_keys.create(root, "factory-a", "dev-pass")
    desk_keys.revoke(root, bench.key_id)
    sent = []

    class FakeApi:
        def __init__(self, cfg):
            assert (cfg.server_url, cfg.token) == ("https://ota.test", "tok")

        def put_desk_key(self, entry):
            sent.append((entry.key_id, entry.revoked))

    monkeypatch.setattr("openmv_ota.client.api.Api", FakeApi)
    assert main(["project", "keys", "desk", "push", "--server", "https://ota.test",
                 "--token", "tok", str(root)]) == 0
    assert sent == [(bench.key_id, True), (factory.key_id, False)]
    assert "Pushed 2 desk keys" in capsys.readouterr().out
    monkeypatch.delenv("OPENMV_OTA_TOKEN", raising=False)
    monkeypatch.setattr("openmv_ota.client.config.load", lambda path=None: None)
    assert main(["project", "keys", "desk", "push", str(root)]) != 0              # no token
    assert "no API token" in capsys.readouterr().err


def test_push_refuses_an_uncertified_live_desk_and_skips_an_uncertified_revoked_one(
        tmp_path, capsys, monkeypatch):
    from openmv_ota.project import issuer
    from tests.project.test_issuer import roots_for
    root = _root(tmp_path)
    assert main(["project", "keys", "desk", "create", "bench-1", str(root)]) == 0   # no issuer yet
    assert "UNCERTIFIED" in capsys.readouterr().out
    assert main(["project", "keys", "desk", "list", str(root)]) == 0
    assert "UNCERTIFIED" in capsys.readouterr().out
    sent = []

    class FakeApi:
        def __init__(self, cfg):
            pass

        def put_desk_key(self, entry):
            sent.append(entry.label)
    monkeypatch.setattr("openmv_ota.client.api.Api", FakeApi)
    push = ["project", "keys", "desk", "push", "--server", "https://ota.test", "--token", "tok",
            str(root)]
    assert main(push) != 0 and sent == []
    assert "'bench-1' are not certified" in capsys.readouterr().err
    # certify needs an issuer, which needs the root
    assert main(["project", "keys", "desk", "certify", str(root)]) != 0
    assert "no issuer key" in capsys.readouterr().err
    roots_for(root, "dev-pass")
    assert main(["project", "keys", "issuer", "show", str(root)]) == 0
    assert "No issuer key" in capsys.readouterr().out
    assert main(["project", "keys", "issuer", "create", str(root)]) == 0
    assert "certified by root 0" in capsys.readouterr().out
    assert main(["project", "keys", "issuer", "create", str(root)]) != 0           # one issuer
    assert "already has an issuer" in capsys.readouterr().err
    assert main(["project", "keys", "issuer", "show", str(root)]) == 0
    out = capsys.readouterr().out
    assert "Issuer key %d" % issuer.read(root).key_id in out and "cert  4f4d5643" in out
    # a revoked, never-certified desk is left alone: the server never had it
    gone = desk_keys.create(root, "gone", "dev-pass")
    desk_keys._write(root, [e if e.label != "gone" else
                            desk_keys.DeskEntry.from_dict({**e.to_dict(), "revoked": True})
                            for e in desk_keys.read(root)])
    for e in desk_keys.read(root):
        if e.label == "gone":
            e.cert = e.issuer_cert = ""
    plain = [desk_keys.DeskEntry.from_dict({k: v for k, v in e.to_dict().items()
                                            if k not in ("cert", "issuer_cert")} | (
        {"revoked": True} if e.label == "gone" else {})) for e in desk_keys.read(root)]
    desk_keys._write(root, plain)
    assert main(["project", "keys", "desk", "certify", str(root)]) == 0
    assert "Certified 1 desk key: bench-1" in capsys.readouterr().out
    assert main(["project", "keys", "desk", "certify", str(root)]) == 0
    assert "certified already" in capsys.readouterr().out
    assert main(push) == 0 and sent == ["bench-1"]
    assert "Pushed 1 desk key." in capsys.readouterr().out
    assert gone.key_id in {e.key_id for e in desk_keys.read(root) if e.revoked}
    assert main(["project", "keys", "issuer", "replace", str(root)]) == 0
    assert "keep the certificates" in capsys.readouterr().out
    from openmv_ota.project import history
    actions = [e["action"] for e in history.read(root)]
    assert actions == ["desk-key-create", "issuer-create", "desk-key-certify", "issuer-replace"]


def test_a_desk_made_with_an_issuer_says_to_push_it(tmp_path, capsys):
    from openmv_ota.project import issuer
    from tests.project.test_issuer import roots_for
    root = _root(tmp_path)
    roots_for(root, "dev-pass")
    issuer.create(root, "dev-pass")
    assert main(["project", "keys", "desk", "create", "bench-1", str(root)]) == 0
    out = capsys.readouterr().out
    assert "server accepts the cameras this desk registers" in out and "UNCERTIFIED" not in out
    assert desk_keys.read(root)[0].certified


def test_roots_push_and_show(tmp_path, capsys, monkeypatch):
    from tests.project.test_issuer import roots_for
    root = _root(tmp_path)
    pinned = {}

    class FakeApi:
        def __init__(self, cfg):
            assert (cfg.server_url, cfg.token) == ("https://ota.test", "tok")

        def put_roots(self, roots, statement=None):
            replaced = bool(pinned) and pinned.get("roots") != roots
            pinned["roots"], pinned["statement"] = roots, statement
            return {"roots": roots, "replaced": replaced}

        def roots(self):
            return pinned.get("roots", [])
    monkeypatch.setattr("openmv_ota.client.api.Api", FakeApi)
    net = ["--server", "https://ota.test", "--token", "tok", str(root)]
    assert main(["project", "keys", "roots", "push"] + net) != 0           # no roots yet
    assert "no root_keys.json" in capsys.readouterr().err
    assert main(["project", "keys", "roots", "show"] + net) != 0
    assert "no root_keys.json" in capsys.readouterr().err
    roots_for(root, "dev-pass")
    assert main(["project", "keys", "roots", "show"] + net) == 1
    assert "no pinned roots" in capsys.readouterr().out
    assert main(["project", "keys", "roots", "push"] + net) == 0
    assert "Pinned the account to 2 roots." in capsys.readouterr().out
    assert [r["root_id"] for r in pinned["roots"]] == [0, 1] and pinned["statement"] is None
    assert main(["project", "keys", "roots", "show"] + net) == 0
    out = capsys.readouterr().out
    assert out.count("match") == 2
    # a rotation: the OLD root signs the statement for the new set
    old_pem = tmp_path / "old-root-1.pem"
    old_pem.write_bytes((root / "keys" / "private" / "root-1.pem").read_bytes())
    (tmp_path / "root-1.pem").write_bytes(old_pem.read_bytes())
    roots_for(root, "dev-pass")                                            # new roots in the project
    assert main(["project", "keys", "roots", "show"] + net) == 1
    assert "DIFFER" in capsys.readouterr().out
    assert main(["project", "keys", "roots", "push", "--rotation-key", str(old_pem)] + net) != 0
    assert "root-<id>.pem" in capsys.readouterr().err
    from openmv_ota.project import config as cfg
    text = cfg.render_config("p", None, ["OPENMV_N6"])
    (root / "openmv-ota.toml").write_text(text)
    assert main(["project", "keys", "roots", "push", "--rotation-key",
                 str(tmp_path / "root-1.pem")] + net) != 0                 # no account id configured
    assert "account_id" in capsys.readouterr().err
    (root / "openmv-ota.toml").write_text(text.replace('[product]\n',
                                                       '[product]\naccount_id = "acct_x"\n', 1))
    assert main(["project", "keys", "roots", "push", "--rotation-key",
                 str(tmp_path / "root-1.pem")] + net) == 0
    assert "Re-pinned the account to 2 roots (statement signed by root 1)" in \
        capsys.readouterr().out
    assert pinned["statement"]["signed_by"] == 1
    assert main(["project", "keys", "roots", "push", "--rotation-key",
                 str(tmp_path / "nope-1.pem")] + net) != 0
    assert "cannot read" in capsys.readouterr().err
    from openmv_ota.ota.algorithms import ES256, algorithm_for
    from openmv_ota.ota.keys import generate_private_key, private_key_pem
    (tmp_path / "root-0.pem").write_bytes(
        private_key_pem(generate_private_key(algorithm_for(ES256)), "another-passphrase"))
    assert main(["project", "keys", "roots", "push", "--rotation-key",
                 str(tmp_path / "root-0.pem")] + net) != 0                 # wrong passphrase
    assert "could not load private key" in capsys.readouterr().err

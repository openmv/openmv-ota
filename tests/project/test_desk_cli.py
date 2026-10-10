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
    assert "Desk key 1 for 'bench-1'" in out and "desk push" in out
    assert main(["project", "keys", "desk", "create", "bench-1", root]) != 0      # one per desk
    assert "already has a key" in capsys.readouterr().err
    assert main(["project", "keys", "desk", "list", root]) == 0
    assert "bench-1" in capsys.readouterr().out
    assert main(["project", "keys", "desk", "renew", "1", root]) == 0
    assert "now expires" in capsys.readouterr().out
    assert main(["project", "keys", "desk", "revoke", "1", root]) == 0
    assert "Revoked desk key 1" in capsys.readouterr().out
    assert main(["project", "keys", "desk", "list", root]) == 0
    assert "revoked" in capsys.readouterr().out
    assert main(["project", "keys", "desk", "renew", "1", root]) != 0             # revoked
    assert main(["project", "keys", "desk", "revoke", "7", root]) != 0
    from openmv_ota.project import history
    actions = [e["action"] for e in history.read(root)]
    assert actions == ["desk-key-create", "desk-key-renew", "desk-key-revoke"]


def test_push_sends_every_desk_key(tmp_path, capsys, monkeypatch):
    root = _root(tmp_path)
    desk_keys.create(root, "bench-1", "dev-pass")
    desk_keys.create(root, "factory-a", "dev-pass")
    desk_keys.revoke(root, 1)
    sent = []

    class FakeApi:
        def __init__(self, cfg):
            assert (cfg.server_url, cfg.token) == ("https://ota.test", "tok")

        def put_desk_key(self, entry):
            sent.append((entry.key_id, entry.revoked))

    monkeypatch.setattr("openmv_ota.client.api.Api", FakeApi)
    assert main(["project", "keys", "desk", "push", "--server", "https://ota.test",
                 "--token", "tok", str(root)]) == 0
    assert sent == [(1, True), (2, False)] and "Pushed 2 desk keys" in capsys.readouterr().out
    monkeypatch.delenv("OPENMV_OTA_TOKEN", raising=False)
    monkeypatch.setattr("openmv_ota.client.config.load", lambda path=None: None)
    assert main(["project", "keys", "desk", "push", str(root)]) != 0              # no token
    assert "no API token" in capsys.readouterr().err

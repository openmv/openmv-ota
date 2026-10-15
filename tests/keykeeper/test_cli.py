"""``openmv-ota keykeeper ...``: init, chain, pin, rotate, serve and restore, configured from the
environment the way a container runs it."""

from __future__ import annotations

import json

from openmv_ota.cli import main
from openmv_ota.keykeeper import cli as kc
from openmv_ota.keykeeper import ring as kr


def _env(monkeypatch, tmp_path, *, passphrase="pass", token="tok"):
    monkeypatch.setenv(kc.DIR_VAR, str(tmp_path / "kk"))
    monkeypatch.setenv(kc.PASSPHRASE_VAR, passphrase)
    monkeypatch.setenv(kc.TOKEN_VAR, token)
    monkeypatch.setenv(kc.PERIOD_VAR, "30")


def test_init_chain_pin_rotate(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, tmp_path)
    assert main(["keykeeper", "init"]) == 0
    out = capsys.readouterr().out
    assert "generation 1 now, generation 2 from" in out
    pin = out.strip().splitlines()[-1].strip()
    assert main(["keykeeper", "init"]) == 1 and "already holds" in capsys.readouterr().err
    assert main(["keykeeper", "pin"]) == 0 and capsys.readouterr().out.strip() == pin
    assert main(["keykeeper", "chain"]) == 0
    chain = json.loads(capsys.readouterr().out)["chain"]
    assert [g.key_id for g in kr.verify_chain(chain, pin)] == [1, 2]
    assert main(["keykeeper", "rotate", "--now"]) == 0
    assert "Generation 3 is in effect now" in capsys.readouterr().out


def test_a_passphrase_file_works_and_a_missing_passphrase_is_refused(tmp_path, monkeypatch,
                                                                     capsys):
    _env(monkeypatch, tmp_path, passphrase="")
    try:
        main(["keykeeper", "init"])
        raise AssertionError("init without a passphrase")
    except SystemExit as e:
        assert "OPENMV_KEYKEEPER_PASSPHRASE" in str(e)
    pf = tmp_path / "pw"
    pf.write_text("pass\n")
    assert main(["keykeeper", "init", "--passphrase-file", str(pf)]) == 0


def test_errors_without_keys(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, tmp_path)
    for cmd in (["chain"], ["pin"], ["rotate", "--now"]):
        assert main(["keykeeper", *cmd]) == 1
        assert "keykeeper init" in capsys.readouterr().err
    monkeypatch.setenv(kc.PERIOD_VAR, "yearly")
    try:
        main(["keykeeper", "init"])
        raise AssertionError("bad period")
    except SystemExit as e:
        assert "whole number of days" in str(e)


def test_serve_builds_the_app_and_needs_a_token(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, tmp_path)
    main(["keykeeper", "init"])
    served = []
    monkeypatch.setattr(kc, "_serve", lambda app, host, port: served.append((host, port)))
    assert main(["keykeeper", "serve", "--port", "9000"]) == 0
    assert served == [("127.0.0.1", 9000)]
    monkeypatch.setenv(kc.TOKEN_VAR, "")
    assert main(["keykeeper", "serve"]) == 1 and "shared secret" in capsys.readouterr().err


def test_serve_init_makes_the_keys_once(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, tmp_path)
    monkeypatch.setattr(kc, "_serve", lambda app, host, port: None)
    assert main(["keykeeper", "serve", "--init"]) == 0
    pin = capsys.readouterr().out.strip().rsplit(" ", 1)[1]
    assert main(["keykeeper", "pin"]) == 0 and capsys.readouterr().out.strip() == pin
    assert main(["keykeeper", "serve", "--init"]) == 0               # keys kept, not replaced
    assert "keys made" not in capsys.readouterr().out
    assert main(["keykeeper", "pin"]) == 0 and capsys.readouterr().out.strip() == pin


def test_serve_hands_the_backup_token_to_the_backup_route(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    _env(monkeypatch, tmp_path)
    main(["keykeeper", "init"])
    apps = []
    monkeypatch.setattr(kc, "_serve", lambda app, host, port: apps.append(app))
    assert main(["keykeeper", "serve"]) == 0
    assert TestClient(apps[-1]).get("/v1/backup").status_code == 404     # no backup token
    monkeypatch.setenv(kc.BACKUP_TOKEN_VAR, "bk")
    assert main(["keykeeper", "serve"]) == 0
    r = TestClient(apps[-1]).get("/v1/backup", headers={"Authorization": "Bearer bk"})
    assert r.status_code == 200 and kr.read_backup(r.content)


def test_restore_puts_a_backup_into_an_empty_directory(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, tmp_path)
    main(["keykeeper", "init"])
    pin = capsys.readouterr().out.strip().splitlines()[-1].strip()
    archive = tmp_path / "kk.tar.gz"
    archive.write_bytes(kr.Ring(tmp_path / "kk", "pass").backup())
    monkeypatch.setenv(kc.DIR_VAR, str(tmp_path / "fresh"))
    assert main(["keykeeper", "restore", str(archive)]) == 0
    out = capsys.readouterr().out
    assert "generations 1 to 2" in out and out.strip().splitlines()[-1].strip() == pin
    assert main(["keykeeper", "pin"]) == 0 and capsys.readouterr().out.strip() == pin
    assert main(["keykeeper", "restore", str(archive)]) == 1
    assert "isn't empty" in capsys.readouterr().err
    assert main(["keykeeper", "restore", str(tmp_path / "missing.tar.gz")]) == 1
    assert "can't read" in capsys.readouterr().err
    monkeypatch.setenv(kc.PASSPHRASE_VAR, "")
    try:
        main(["keykeeper", "restore", str(archive)])
        raise AssertionError("restore without a passphrase")
    except SystemExit as e:
        assert "OPENMV_KEYKEEPER_PASSPHRASE" in str(e)

"""``openmv-ota keykeeper ...``: init, chain, pin, rotate and serve, configured from the
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

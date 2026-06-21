"""Tests for the ``openmv-ota project`` CLI."""

from __future__ import annotations

from openmv_ota.cli import main
from openmv_ota.project import project as proj


def _new(tmp_path, make_firmware, make_sdk, *extra, root="proj", repo=None):
    repo = repo or make_firmware()
    home = make_sdk()
    rc = main(["project", "new", str(tmp_path / root),
               "-f", str(repo), "-b", "OPENMV_N6", "-b", "OPENMV_AE3",
               "--sdk-home", str(home), "--allow-dirty", *extra])
    return rc, tmp_path / root, repo


def test_new_success(tmp_path, make_firmware, make_sdk, capsys):
    rc, root, _ = _new(tmp_path, make_firmware, make_sdk, "--product", "P", "--vendor", "V")
    assert rc == 0
    out = capsys.readouterr().out
    assert "firmware:    5.0.0" in out and "vela 5.0.0" in out
    assert (root / "openmv-ota.toml").exists()


def test_new_not_git(tmp_path, make_sdk, capsys):
    rc = main(["project", "new", str(tmp_path / "p"), "-f", str(tmp_path / "nope"),
               "-b", "OPENMV_N6", "--sdk-home", str(make_sdk())])
    assert rc == 2
    assert "not a git repository" in capsys.readouterr().err


def test_new_sdk_missing(tmp_path, make_firmware, capsys):
    rc = main(["project", "new", str(tmp_path / "p"), "-f", str(make_firmware()),
               "-b", "OPENMV_N6", "--sdk-home", str(tmp_path / "nosdk")])
    assert rc == 2
    assert "not installed" in capsys.readouterr().err


def test_new_existing_no_force(tmp_path, make_firmware, make_sdk, capsys):
    repo = make_firmware()
    _new(tmp_path, make_firmware, make_sdk, repo=repo)
    capsys.readouterr()
    rc, _, _ = _new(tmp_path, make_firmware, make_sdk, repo=repo)
    assert rc == 1
    assert "already exists" in capsys.readouterr().err


def test_new_force(tmp_path, make_firmware, make_sdk):
    repo = make_firmware()
    _new(tmp_path, make_firmware, make_sdk, repo=repo)
    rc, _, _ = _new(tmp_path, make_firmware, make_sdk, "--force", repo=repo)
    assert rc == 0


def test_show(tmp_path, make_firmware, make_sdk, capsys):
    _, root, _ = _new(tmp_path, make_firmware, make_sdk)
    capsys.readouterr()
    assert main(["project", "show", str(root)]) == 0
    assert "micropython: 1.28.0" in capsys.readouterr().out


def test_show_json(tmp_path, make_firmware, make_sdk, capsys):
    _, root, _ = _new(tmp_path, make_firmware, make_sdk)
    capsys.readouterr()
    assert main(["project", "show", str(root), "--json"]) == 0
    assert '"lock_schema_version": 1' in capsys.readouterr().out


def test_show_no_lock(tmp_path, capsys):
    rc = main(["project", "show", str(tmp_path)])
    assert rc == 2
    assert "no openmv-ota.lock.json" in capsys.readouterr().err


def test_status_in_sync(tmp_path, make_firmware, make_sdk, capsys):
    repo = make_firmware()
    _, root, _ = _new(tmp_path, make_firmware, make_sdk, repo=repo)
    capsys.readouterr()
    assert main(["project", "status", str(root), "-f", str(repo)]) == 0
    assert "in sync" in capsys.readouterr().out


def test_status_drift(tmp_path, make_firmware, make_sdk, git_cmd, capsys):
    repo = make_firmware()
    _, root, _ = _new(tmp_path, make_firmware, make_sdk, repo=repo)
    (repo / "x.txt").write_text("x")
    git_cmd(repo, "add", "-A")
    git_cmd(repo, "commit", "-q", "-m", "c2")
    capsys.readouterr()
    rc = main(["project", "status", str(root), "-f", str(repo)])
    assert rc == 1
    assert "drift detected" in capsys.readouterr().out


def test_status_quiet(tmp_path, make_firmware, make_sdk, git_cmd, capsys):
    repo = make_firmware()
    _, root, _ = _new(tmp_path, make_firmware, make_sdk, repo=repo)
    (repo / "x.txt").write_text("x")
    git_cmd(repo, "add", "-A")
    git_cmd(repo, "commit", "-q", "-m", "c2")
    capsys.readouterr()
    assert main(["project", "status", str(root), "-f", str(repo), "-q"]) == 1
    assert capsys.readouterr().out == ""


def test_status_no_checkout(tmp_path, make_firmware, make_sdk, capsys):
    _, root, _ = _new(tmp_path, make_firmware, make_sdk)
    proj.ProjectPaths(root).local.unlink()
    rc = main(["project", "status", str(root)])
    assert rc == 2
    assert "no firmware checkout" in capsys.readouterr().err


def test_sync(tmp_path, make_firmware, make_sdk, capsys):
    repo = make_firmware()
    _, root, _ = _new(tmp_path, make_firmware, make_sdk, repo=repo)
    capsys.readouterr()
    rc = main(["project", "sync", str(root), "-f", str(repo), "--sdk-home", str(make_sdk()),
               "--allow-dirty"])
    assert rc == 0
    assert "Re-locked" in capsys.readouterr().out


def test_sync_error(tmp_path, make_firmware, make_sdk, capsys):
    _, root, _ = _new(tmp_path, make_firmware, make_sdk)
    proj.ProjectPaths(root).local.unlink()
    rc = main(["project", "sync", str(root)])
    assert rc == 2
    assert "no firmware checkout" in capsys.readouterr().err


def test_setup_cli(tmp_path, make_firmware, make_sdk, monkeypatch, capsys):
    _, root, _ = _new(tmp_path, make_firmware, make_sdk)
    proj.ProjectPaths(root).local.unlink()
    monkeypatch.setattr(proj.gitrepo, "is_git_repo", lambda d: False)
    monkeypatch.setattr(proj.gitrepo, "clone", lambda r, d, commit=None: d.mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr(proj.gitrepo, "submodule_update", lambda d: None)
    rc = main(["project", "setup", str(root), "--cache", str(tmp_path / "cache"), "--no-install-sdk"])
    assert rc == 0
    assert "Firmware ready" in capsys.readouterr().out
    assert proj.ProjectPaths(root).local.exists()


def test_setup_cli_error(tmp_path, capsys):
    rc = main(["project", "setup", str(tmp_path)])
    assert rc == 2
    assert "no openmv-ota.lock.json" in capsys.readouterr().err


def test_verify_cli_pass(tmp_path, make_firmware, make_sdk, capsys):
    repo = make_firmware()
    _, root, _ = _new(tmp_path, make_firmware, make_sdk, repo=repo)
    capsys.readouterr()
    assert main(["project", "verify", str(root), "-f", str(repo)]) == 0
    assert "verified" in capsys.readouterr().out


def test_verify_cli_fail(tmp_path, make_firmware, make_sdk, git_cmd, capsys):
    repo = make_firmware()
    _, root, _ = _new(tmp_path, make_firmware, make_sdk, repo=repo)
    (repo / "x.txt").write_text("x")
    git_cmd(repo, "add", "-A")
    git_cmd(repo, "commit", "-q", "-m", "c2")
    capsys.readouterr()
    rc = main(["project", "verify", str(root), "-f", str(repo)])
    assert rc == 1
    assert "verification failed" in capsys.readouterr().err


def test_verify_cli_error(tmp_path, capsys):
    rc = main(["project", "verify", str(tmp_path)])
    assert rc == 2
    assert "no openmv-ota.lock.json" in capsys.readouterr().err

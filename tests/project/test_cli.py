"""Tests for the ``openmv-ota project`` CLI."""

from __future__ import annotations

from openmv_ota.cli import main
from openmv_ota.project import project as proj


def _new(tmp_path, make_firmware, make_sdk, *extra, root="proj", repo=None):
    repo = repo or make_firmware()
    home = make_sdk()
    # OTA projects need a key passphrase; default to throwaway --dev unless the test set its own.
    if "--ota" in extra and "--dev" not in extra and "--key-passphrase-file" not in extra:
        extra = (*extra, "--dev")
    rc = main(["project", "new", str(tmp_path / root),
               "-f", str(repo), "-b", "OPENMV_N6", "-b", "OPENMV_AE3",
               "--sdk-home", str(home), "--allow-dirty", *extra])
    return rc, tmp_path / root, repo


def test_new_success(tmp_path, make_firmware, make_sdk, capsys):
    rc, root, _ = _new(tmp_path, make_firmware, make_sdk, "--product", "P", "--vendor", "V")
    assert rc == 0
    out = capsys.readouterr().out
    assert "firmware:    5.0.0" in out and "vela 5.0.0" in out
    assert "mode:        single image" in out
    assert (root / "openmv-ota.toml").exists()


def test_new_ota(tmp_path, make_firmware, make_sdk, capsys):
    rc, root, _ = _new(tmp_path, make_firmware, make_sdk, "--ota")
    assert rc == 0
    assert "mode:        OTA" in capsys.readouterr().out
    capsys.readouterr()
    assert main(["project", "show", str(root)]) == 0
    assert "mode:        OTA" in capsys.readouterr().out


def test_new_ota_with_key_passphrase_file(tmp_path, make_firmware, make_sdk):
    pf = tmp_path / "kp"
    pf.write_text("my-real-passphrase")
    rc, root, _ = _new(tmp_path, make_firmware, make_sdk, "--ota", "--key-passphrase-file", str(pf))
    assert rc == 0 and not (root / "keys" / ".dev-passphrase").exists()   # non-dev: no dev cache


def test_new_ota_requires_a_passphrase(tmp_path, make_firmware, make_sdk, capsys):
    repo, home = make_firmware(), make_sdk()
    rc = main(["project", "new", str(tmp_path / "p"), "-f", str(repo), "-b", "OPENMV_N6",
               "--sdk-home", str(home), "--allow-dirty", "--ota"])   # no --dev, no passphrase
    assert rc != 0 and "encrypted" in capsys.readouterr().err


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


def test_new_ota_dev_skips_the_backup(tmp_path, make_firmware, make_sdk, capsys):
    # dev keys are disposable and their cached passphrase sits beside them, so a backup
    # would be effectively plaintext -- new skips it silently for --dev projects.
    rc, root, _ = _new(tmp_path, make_firmware, make_sdk, "--ota")   # _new defaults to --dev
    assert rc == 0
    assert not (root / "keys-backup.bin").exists()
    assert "backup" not in capsys.readouterr().out.lower()


def test_new_ota_always_backs_up_real_keys(tmp_path, make_firmware, make_sdk, capsys):
    pw = tmp_path / "pw.txt"
    pw.write_text("a strong passphrase")
    rc, root, _ = _new(tmp_path, make_firmware, make_sdk, "--ota",
                       "--key-passphrase-file", str(pw))
    assert rc == 0
    assert (root / "keys-backup.bin").exists()
    assert "MOVE IT OFF THIS MACHINE" in capsys.readouterr().out


def test_new_ota_backup_failure_warns_but_succeeds(tmp_path, make_firmware, make_sdk, capsys):
    # a backup that cannot be written shouldn't fail the project (it was created); just warn
    pw = tmp_path / "pw.txt"
    pw.write_text("a strong passphrase")
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "keys-backup.bin").mkdir()   # write_bytes() will fail on a directory
    rc, root, _ = _new(tmp_path, make_firmware, make_sdk, "--ota",
                       "--key-passphrase-file", str(pw))
    assert rc == 0 and (root / "openmv-ota.toml").exists()
    assert "key backup skipped" in capsys.readouterr().err


def test_new_force_warns_key_regeneration(tmp_path, make_firmware, make_sdk, capsys):
    repo = make_firmware()
    _new(tmp_path, make_firmware, make_sdk, "--ota", repo=repo)
    capsys.readouterr()
    rc, _, _ = _new(tmp_path, make_firmware, make_sdk, "--ota", "--force", repo=repo)
    assert rc == 0
    assert "REJECT updates signed by the new ones" in capsys.readouterr().err


# --- keys backup / restore ---------------------------------------------------

def _new_real_keys(tmp_path, make_firmware, make_sdk):
    """A non-dev OTA project (real passphrase-encrypted keys), whose backup is allowed."""
    pw = tmp_path / "kp.txt"
    pw.write_text("vault passphrase")
    rc, root, _ = _new(tmp_path, make_firmware, make_sdk, "--ota",
                       "--key-passphrase-file", str(pw))
    assert rc == 0
    return root


def test_keys_backup_and_restore_roundtrip(tmp_path, make_firmware, make_sdk, capsys):
    root = _new_real_keys(tmp_path, make_firmware, make_sdk)
    capsys.readouterr()
    assert main(["project", "keys", "backup", str(root)]) == 0
    assert "MOVE IT OFF THIS MACHINE" in capsys.readouterr().out
    backup = root / "keys-backup.bin"
    assert backup.exists()

    # wipe the private keys, then restore them from the backup -- no passphrase involved:
    # the PEMs inside are archived exactly as they sit on disk, already encrypted at rest.
    private = proj.ProjectPaths(root).private_keys_dir
    saved = {p.name: p.read_bytes() for p in private.glob("*.pem")}
    for p in private.glob("*.pem"):
        p.unlink()
    rc = main(["project", "keys", "restore", str(backup), str(root)])
    assert rc == 0 and "Restored" in capsys.readouterr().out
    assert {p.name: p.read_bytes() for p in private.glob("*.pem")} == saved


def test_keys_backup_refuses_dev_projects(tmp_path, make_firmware, make_sdk, capsys):
    _rc, root, _ = _new(tmp_path, make_firmware, make_sdk, "--ota")   # --dev by default
    rc = main(["project", "keys", "backup", str(root)])
    assert rc == 1 and "refusing to back up a --dev project" in capsys.readouterr().err


def test_keys_backup_no_keys_errors(tmp_path, make_firmware, make_sdk, capsys):
    _rc, root, _ = _new(tmp_path, make_firmware, make_sdk)   # non-OTA -> no private keys
    rc = main(["project", "keys", "backup", str(root)])
    assert rc == 1 and "no private keys" in capsys.readouterr().err


def test_keys_restore_corrupt_backup(tmp_path, make_firmware, make_sdk, capsys):
    root = _new_real_keys(tmp_path, make_firmware, make_sdk)
    main(["project", "keys", "backup", str(root)])
    backup = root / "keys-backup.bin"
    blob = bytearray(backup.read_bytes())
    blob[-1] ^= 0xFF                                          # flip a payload byte
    backup.write_bytes(bytes(blob))
    capsys.readouterr()
    rc = main(["project", "keys", "restore", str(backup), str(root)])
    assert rc == 2 and "corrupt key backup" in capsys.readouterr().err


def test_keys_restore_missing_backup(tmp_path, make_firmware, make_sdk, capsys):
    root = _new_real_keys(tmp_path, make_firmware, make_sdk)
    rc = main(["project", "keys", "restore", str(tmp_path / "nope.bin"), str(root)])
    assert rc == 2 and "can't read backup" in capsys.readouterr().err


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


def _new_ota(tmp_path, make_firmware, make_sdk, capsys):
    rc, root, repo = _new(tmp_path, make_firmware, make_sdk, "--ota",
                          "--ota-keys", "4", "--factory-keys", "2")
    assert rc == 0
    capsys.readouterr()  # drain `new` output
    return root, repo


def test_keys_status(tmp_path, make_firmware, make_sdk, capsys):
    root, _ = _new_ota(tmp_path, make_firmware, make_sdk, capsys)
    assert main(["project", "keys", "status", str(root)]) == 0
    out = capsys.readouterr().out
    assert "signing key:" in out and "ota pool:" in out


def test_keys_rotate(tmp_path, make_firmware, make_sdk, capsys):
    root, _ = _new_ota(tmp_path, make_firmware, make_sdk, capsys)
    assert main(["project", "keys", "rotate", str(root)]) == 0
    assert "0x0100 -> 0x0101" in capsys.readouterr().out


def test_keys_revoke_unrevoke(tmp_path, make_firmware, make_sdk, capsys):
    root, _ = _new_ota(tmp_path, make_firmware, make_sdk, capsys)
    assert main(["project", "keys", "revoke", "0x0102", str(root)]) == 0
    assert "Revoked key 0x0102" in capsys.readouterr().out
    assert main(["project", "keys", "unrevoke", "0x0102", str(root)]) == 0
    assert "Unrevoked key 0x0102" in capsys.readouterr().out


def test_keys_revoke_current_signer_warns(tmp_path, make_firmware, make_sdk, capsys):
    root, _ = _new_ota(tmp_path, make_firmware, make_sdk, capsys)
    assert main(["project", "keys", "revoke", "0x0100", str(root)]) == 0
    assert "current signing key" in capsys.readouterr().err


def test_keys_status_non_ota_errors(tmp_path, make_firmware, make_sdk, capsys):
    rc, root, _ = _new(tmp_path, make_firmware, make_sdk)  # non-OTA project
    assert rc == 0
    capsys.readouterr()
    assert main(["project", "keys", "status", str(root)]) != 0
    assert "not an OTA project" in capsys.readouterr().err


def test_keys_mutations_require_ota(tmp_path, make_firmware, make_sdk, capsys):
    rc, root, _ = _new(tmp_path, make_firmware, make_sdk)  # non-OTA
    assert rc == 0
    capsys.readouterr()
    assert main(["project", "keys", "rotate", str(root)]) != 0
    assert main(["project", "keys", "revoke", "0x0100", str(root)]) != 0
    assert main(["project", "keys", "unrevoke", "0x0100", str(root)]) != 0
    assert "not an OTA project" in capsys.readouterr().err


def test_keys_revoke_idempotent_and_unrevoke_noop(tmp_path, make_firmware, make_sdk, capsys):
    root, _ = _new_ota(tmp_path, make_firmware, make_sdk, capsys)
    assert main(["project", "keys", "revoke", "0x0102", str(root)]) == 0
    capsys.readouterr()
    assert main(["project", "keys", "revoke", "0x0102", str(root)]) == 0  # again
    assert "already revoked" in capsys.readouterr().out
    assert main(["project", "keys", "unrevoke", "0x0103", str(root)]) == 0  # never revoked
    assert "is not revoked" in capsys.readouterr().out

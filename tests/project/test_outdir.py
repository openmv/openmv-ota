"""A project's build output is its own files."""

from __future__ import annotations

import pytest

from openmv_ota.build.errors import BuildError
from openmv_ota.project.errors import ProjectError
from openmv_ota.project.outdir import output_dir


def test_the_output_is_the_one_named_or_the_project_s_build(tmp_path):
    assert output_dir(tmp_path, None) == tmp_path / "build"          # not made yet: fine
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "OPENMV_N6-firmware.bin").write_bytes(b"x")
    assert output_dir(str(tmp_path), None) == tmp_path / "build"
    assert output_dir(tmp_path, str(tmp_path / "out")) == tmp_path / "out"


def test_a_link_in_a_project_s_build_is_refused(tmp_path):
    """A project someone else made can ship `build/<board>-bootloader.bin` as a link: the
    build wrote through it, and a link to a file of the user's was packed and published."""
    secret = tmp_path / "secret"
    secret.write_bytes(b"mine")
    build = tmp_path / "p" / "build"
    build.mkdir(parents=True)
    (build / "OPENMV_N6-bootloader.bin").symlink_to(secret)
    with pytest.raises(ProjectError, match="is a link"):
        output_dir(tmp_path / "p", None)
    with pytest.raises(BuildError, match="is a link"):               # read from it either way
        output_dir(tmp_path / "p", tmp_path / "out", BuildError)
    (build / "OPENMV_N6-bootloader.bin").unlink()
    assert output_dir(tmp_path / "p", tmp_path / "out") == tmp_path / "out"
    linked = tmp_path / "q"
    linked.mkdir()
    (linked / "build").symlink_to(tmp_path)
    with pytest.raises(ProjectError, match="build is a link"):
        output_dir(linked, None)


def test_a_project_s_own_files_are_not_links(tmp_path):
    """The lock, the ledger and the key files are written with what the project says; the
    build output is read back, packed and published. Deep in `build/` counts, and so does
    an output directory named inside the project."""
    from openmv_ota.project import config as config_mod
    from openmv_ota.project.outdir import check_project
    mine = tmp_path / "mine"
    mine.write_text("mine")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    n = 0
    for place in ("openmv-ota.lock", ".openmv-ota/ledger.json", ".openmv-ota",
                  "build/releases/OPENMV_N6-ota-1.0.0.img.gz", "keys/desk_keys.json",
                  "keys/private/dangling", "keys/private", "keys"):
        n += 1
        root = tmp_path / ("p%d" % n)
        (root / place).parent.mkdir(parents=True)
        (root / place).symlink_to(elsewhere if place in (".openmv-ota", "keys/private", "keys")
                                  else
                                  tmp_path / "nothing" if "dangling" in place else mine)
        with pytest.raises(ProjectError, match="is a link"):
            check_project(root)
        (root / config_mod.CONFIG_NAME).write_text("")
        with pytest.raises(ProjectError, match="is a link"):       # where every command starts
            config_mod.load_config(root / config_mod.CONFIG_NAME)
    ok = tmp_path / "ok"
    ok.mkdir()
    (ok / "firmware").symlink_to(elsewhere)                        # a checkout beside it: fine
    (ok / ".#openmv-ota.toml").symlink_to("someone@host.1234")     # an editor's lock
    check_project(ok)
    (ok / "app").symlink_to(elsewhere)                             # packed whole: not a link
    with pytest.raises(ProjectError, match="app is a link"):
        check_project(ok)
    (ok / "app").unlink()
    check_project(tmp_path / "not-there")
    (ok / "factory").mkdir()
    assert output_dir(ok, ok / "factory") == ok / "factory"
    (ok / "factory" / "OPENMV_N6-manifest.bin").symlink_to(mine)
    with pytest.raises(BuildError, match="is a link"):
        output_dir(ok, ok / "factory", BuildError)


def test_commands_that_do_not_load_a_project_check_it_too(tmp_path, make_firmware, make_sdk):
    """`project new --force` and `keys backup`/`restore` work on a directory before (or
    without) reading its config, which is where every other command is stopped."""
    from openmv_ota.project import project as proj
    victim = tmp_path / "victim" / "keys"
    victim.mkdir(parents=True)
    (victim / "trusted_keys.json").write_text("theirs")
    root = tmp_path / "p"
    root.mkdir()
    (root / "keys").symlink_to(victim)
    with pytest.raises(ProjectError, match="keys is a link"):
        proj.create_project(root, firmware=make_firmware(), boards=["OPENMV_N6"], product=None,
                            vendor=None, sdk_home_override=make_sdk(), install_sdk=False,
                            allow_dirty=False, force=True, now="t", ota=True, dev=True,
                            new_keys=True)
    with pytest.raises(ProjectError, match="keys is a link"):
        proj.backup_private_keys(root)
    with pytest.raises(ProjectError, match="keys is a link"):
        proj.restore_private_keys(root, b"")
    assert (victim / "trusted_keys.json").read_text() == "theirs"
    assert sorted(p.name for p in root.iterdir()) == ["keys"]          # and nothing written


def test_every_command_checks_the_project_it_names(tmp_path, capsys):
    """The desk, issuer and roots commands never loaded the config, which is where the
    check lived: they wrote `keys/issuer.json` and `keys/desk_keys.json` through a link.
    The check is at the door now, for any command that names a project."""
    from openmv_ota.cli import main
    from openmv_ota.project import config as config_mod
    mine = tmp_path / "mine.json"
    mine.write_text("mine")
    root = tmp_path / "p"
    (root / "keys").mkdir(parents=True)
    (root / config_mod.CONFIG_NAME).write_text("")
    (root / "keys" / "issuer.json").symlink_to(mine)
    for argv in (["project", "keys", "issuer", "replace", str(root)],
                 ["project", "keys", "desk", "list", str(root)],
                 ["project", "show", str(root)],
                 ["flash", "list", str(root)],
                 ["client", "release", "bases", "--project", str(root), "-b", "OPENMV_N6"]):
        assert main(argv) != 0, argv
        assert "is a link" in capsys.readouterr().err, argv
    assert mine.read_text() == "mine"
    # a directory that is no project is nobody's to check (`flash list` in a home directory)
    other = tmp_path / "home"
    other.mkdir()
    (other / ".bashrc").symlink_to(mine)
    from openmv_ota.project.outdir import check_project
    with pytest.raises(ProjectError):
        check_project(other)
    import openmv_ota.cli as cli_mod
    seen = []
    parser = cli_mod.build_parser()
    args = parser.parse_args(["flash", "list", str(other)])
    args.func = lambda a: seen.append(a) or 0
    import argparse
    orig = argparse.ArgumentParser.parse_args
    try:
        argparse.ArgumentParser.parse_args = lambda self, argv=None: args
        assert main(["flash", "list", str(other)]) == 0 and seen
    finally:
        argparse.ArgumentParser.parse_args = orig


def test_the_directories_new_writes_into_hold_no_link(tmp_path):
    """`project new --force` scaffolds into app/, device/, certs/ and compliance/: a link
    in one of them, or one of them as a link, sent a template to a file of the user's."""
    from openmv_ota.project.outdir import check_project
    mine = tmp_path / "mine"
    mine.write_text("mine")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    for n, (place, target) in enumerate((("app/main.py", tmp_path / "nothing"),
                                         ("device/openmv_log.py", mine),
                                         ("certs/my-ca.pem", mine),
                                         ("compliance", elsewhere),
                                         ("app-coprocessor/lib/x.py", mine))):
        root = tmp_path / ("p%d" % n)
        (root / place).parent.mkdir(parents=True)
        (root / place).symlink_to(target)
        with pytest.raises(ProjectError, match="is a link"):
            check_project(root)

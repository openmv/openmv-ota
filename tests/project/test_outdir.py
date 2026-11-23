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

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
    linked = tmp_path / "q"
    linked.mkdir()
    (linked / "build").symlink_to(tmp_path)
    with pytest.raises(ProjectError, match="build is a link"):
        output_dir(linked, None)

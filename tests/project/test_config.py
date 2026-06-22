"""Tests for the committed config and the gitignored local file."""

from __future__ import annotations

from pathlib import Path

import pytest

from openmv_ota.project import config as cfg
from openmv_ota.project.errors import ProjectError


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def test_load_config(tmp_path):
    p = _write(tmp_path / cfg.CONFIG_NAME,
               '[product]\nname="p"\nvendor="v"\n[targets]\nboards=["OPENMV_N6"]\n')
    c = cfg.load_config(p)
    assert c.name == "p" and c.vendor == "v" and c.boards == ["OPENMV_N6"]


def test_load_config_defaults_name_to_dir(tmp_path):
    d = tmp_path / "myproj"
    d.mkdir()
    p = _write(d / cfg.CONFIG_NAME, '[targets]\nboards=["OPENMV_N6"]\n')
    c = cfg.load_config(p)
    assert c.name == "myproj" and c.vendor is None


def test_load_config_with_overrides(tmp_path):
    p = _write(tmp_path / cfg.CONFIG_NAME,
               '[targets]\nboards=["OPENMV_AE3"]\n[targets.OPENMV_AE3]\npartition_size=4096\n')
    c = cfg.load_config(p)
    assert c.overrides == {"OPENMV_AE3": {"partition_size": 4096}}


def test_load_config_missing(tmp_path):
    with pytest.raises(ProjectError, match="no openmv-ota.toml"):
        cfg.load_config(tmp_path / cfg.CONFIG_NAME)


def test_load_config_bad_toml(tmp_path):
    p = _write(tmp_path / cfg.CONFIG_NAME, "not = = toml")
    with pytest.raises(ProjectError, match="not valid TOML"):
        cfg.load_config(p)


def test_load_config_bad_boards(tmp_path):
    p = _write(tmp_path / cfg.CONFIG_NAME, "[targets]\nboards=[]\n")
    with pytest.raises(ProjectError, match="non-empty list"):
        cfg.load_config(p)


def test_load_config_unknown_board(tmp_path):
    p = _write(tmp_path / cfg.CONFIG_NAME, '[targets]\nboards=["NOPE"]\n')
    with pytest.raises(ProjectError, match="unknown board"):
        cfg.load_config(p)


def test_load_local(tmp_path):
    p = _write(tmp_path / cfg.LOCAL_NAME,
               '[firmware]\npath="/abs/openmv"\n[sdk]\nhome="/abs/sdk"\n')
    local = cfg.load_local(p)
    assert local.firmware_path == Path("/abs/openmv") and local.sdk_home == Path("/abs/sdk")


def test_load_local_no_sdk_home(tmp_path):
    p = _write(tmp_path / cfg.LOCAL_NAME, '[firmware]\npath="/abs/openmv"\n[sdk]\nhome=""\n')
    assert cfg.load_local(p).sdk_home is None


def test_load_local_absent(tmp_path):
    assert cfg.load_local(tmp_path / cfg.LOCAL_NAME) is None


def test_load_local_missing_path(tmp_path):
    p = _write(tmp_path / cfg.LOCAL_NAME, "[firmware]\n")
    with pytest.raises(ProjectError, match="missing .firmware..path"):
        cfg.load_local(p)


def test_render_config_roundtrips(tmp_path):
    text = cfg.render_config("prod", "Acme", ["OPENMV_N6", "OPENMV_AE3"])
    p = _write(tmp_path / cfg.CONFIG_NAME, text)
    c = cfg.load_config(p)
    assert c.name == "prod" and c.vendor == "Acme" and c.boards == ["OPENMV_N6", "OPENMV_AE3"]
    assert c.ota is False


def test_render_config_non_ota_leaves_section_commented(tmp_path):
    text = cfg.render_config("prod", None, ["OPENMV_N6"])
    assert "# [ota]" in text
    c = cfg.load_config(_write(tmp_path / cfg.CONFIG_NAME, text))
    assert c.ota is False


def test_render_config_ota_roundtrips(tmp_path):
    text = cfg.render_config("prod", None, ["OPENMV_N6"], ota=True, version=7, signing_key_id=256)
    assert "[ota]\nenabled = true" in text
    c = cfg.load_config(_write(tmp_path / cfg.CONFIG_NAME, text))
    assert c.ota is True and c.version == 7 and c.signing_key_id == 256


def test_non_ota_config_has_default_release_state(tmp_path):
    text = cfg.render_config("prod", None, ["OPENMV_N6"])
    c = cfg.load_config(_write(tmp_path / cfg.CONFIG_NAME, text))
    assert c.version == 1 and c.signing_key_id is None


def test_render_config_no_vendor(tmp_path):
    text = cfg.render_config("prod", None, ["OPENMV_N6"])
    assert "# vendor" in text
    c = cfg.load_config(_write(tmp_path / cfg.CONFIG_NAME, text))
    assert c.vendor is None


def test_render_local_roundtrips(tmp_path):
    text = cfg.render_local(Path("/abs/openmv"), Path("/abs/sdk"))
    local = cfg.load_local(_write(tmp_path / cfg.LOCAL_NAME, text))
    assert local.firmware_path == Path("/abs/openmv") and local.sdk_home == Path("/abs/sdk")


def test_render_local_no_sdk_home(tmp_path):
    text = cfg.render_local(Path("/abs/openmv"), None)
    local = cfg.load_local(_write(tmp_path / cfg.LOCAL_NAME, text))
    assert local.sdk_home is None

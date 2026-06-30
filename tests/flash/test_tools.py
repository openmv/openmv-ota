"""Locating dfu-util: override > SDK bin > PATH > error."""

from __future__ import annotations

import pytest

from openmv_ota.flash import tools
from openmv_ota.flash.errors import FlashError


def test_override_wins(monkeypatch):
    monkeypatch.setattr(tools.shutil, "which", lambda _n: "/usr/bin/dfu-util")
    assert tools.find_dfu_util(override="/my/dfu-util") == "/my/dfu-util"


def test_sdk_bin_preferred_over_path(tmp_path, monkeypatch):
    monkeypatch.setattr(tools.shutil, "which", lambda _n: "/usr/bin/dfu-util")
    binp = tmp_path / "bin"
    binp.mkdir()
    (binp / "dfu-util").write_text("")
    assert tools.find_dfu_util(sdk_home=tmp_path) == str(binp / "dfu-util")


def test_sdk_without_binary_falls_back_to_path(tmp_path, monkeypatch):
    monkeypatch.setattr(tools.shutil, "which", lambda _n: "/usr/bin/dfu-util")
    assert tools.find_dfu_util(sdk_home=tmp_path) == "/usr/bin/dfu-util"


def test_path_lookup(monkeypatch):
    monkeypatch.setattr(tools.shutil, "which", lambda _n: "/usr/bin/dfu-util")
    assert tools.find_dfu_util() == "/usr/bin/dfu-util"


def test_not_found_raises(monkeypatch):
    monkeypatch.setattr(tools.shutil, "which", lambda _n: None)
    with pytest.raises(FlashError, match="dfu-util not found"):
        tools.find_dfu_util()


def test_find_spsdk_prefers_sdk_python_bin(tmp_path, monkeypatch):
    monkeypatch.setattr(tools.shutil, "which", lambda _n: "/usr/bin/blhost")
    binp = tmp_path / "python" / "bin"
    binp.mkdir(parents=True)
    (binp / "blhost").write_text("")
    assert tools.find_spsdk("blhost", sdk_home=tmp_path) == str(binp / "blhost")


def test_find_spsdk_falls_back_to_path(tmp_path, monkeypatch):
    monkeypatch.setattr(tools.shutil, "which", lambda _n: "/usr/bin/sdphost")
    assert tools.find_spsdk("sdphost", sdk_home=tmp_path) == "/usr/bin/sdphost"


def test_find_spsdk_not_found_raises(monkeypatch):
    monkeypatch.setattr(tools.shutil, "which", lambda _n: None)
    with pytest.raises(FlashError, match="blhost not found"):
        tools.find_spsdk("blhost")

"""Tests for the setup clone-cache path resolution."""

from __future__ import annotations

from pathlib import Path

from openmv_ota.project import cache


def test_cache_root_override(tmp_path):
    assert cache.cache_root(str(tmp_path / "c")) == tmp_path / "c"


def test_cache_root_env(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENMV_OTA_CACHE", str(tmp_path / "envcache"))
    assert cache.cache_root() == tmp_path / "envcache"


def test_cache_root_windows(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENMV_OTA_CACHE", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    assert cache.cache_root(os_name="nt") == tmp_path / "appdata" / "openmv-ota" / "cache"


def test_cache_root_windows_no_localappdata(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENMV_OTA_CACHE", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert cache.cache_root(os_name="nt") == tmp_path / "AppData" / "Local" / "openmv-ota" / "cache"


def test_cache_root_xdg(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENMV_OTA_CACHE", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert cache.cache_root(os_name="posix") == tmp_path / "xdg" / "openmv-ota"


def test_cache_root_posix_default(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENMV_OTA_CACHE", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert cache.cache_root(os_name="posix") == tmp_path / ".cache" / "openmv-ota"


def test_firmware_clone_dir(tmp_path):
    d = cache.firmware_clone_dir("abcdef0123456789", str(tmp_path))
    assert d == tmp_path / "openmv-abcdef012345"

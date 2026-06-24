"""Tests for the pure-Python SDK installer (no network: served over file://)."""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from openmv_ota.project import sdk_install as si
from openmv_ota.project.errors import ProjectError

VERSION = "1.0.0"
PLAT = "linux-x86_64"
NAME = "openmv-sdk-%s-%s.tar.xz" % (VERSION, PLAT)


def _make_bundle(top: str, members: dict[str, bytes]) -> bytes:
    """A .tar.xz whose single top-level dir is ``top`` (so strip-1 removes it)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tf:
        top_dir = tarfile.TarInfo(top)        # the bare top-level dir entry (strip-1 drops it)
        top_dir.type = tarfile.DIRTYPE
        top_dir.mode = 0o755
        tf.addfile(top_dir)
        for rel, data in members.items():
            info = tarfile.TarInfo("%s/%s" % (top, rel))
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _publish(serve_dir: Path, archive: bytes, *, checksum: str | None = None) -> str:
    """Write the archive + its .sha256 under serve_dir; return a file:// base URL."""
    (serve_dir / NAME).write_bytes(archive)
    digest = checksum if checksum is not None else hashlib.sha256(archive).hexdigest()
    (serve_dir / (NAME + ".sha256")).write_text("%s  %s\n" % (digest, NAME))
    return serve_dir.as_uri()


# --- sdk_platform ----------------------------------------------------------

@pytest.mark.parametrize(("system", "machine", "expected"), [
    ("Linux", "x86_64", "linux-x86_64"),
    ("Darwin", "arm64", "darwin-arm64"),
    ("Windows", "AMD64", "windows-x86_64"),
    ("Linux", "aarch64", "linux-arm64"),
])
def test_sdk_platform(monkeypatch, system, machine, expected):
    monkeypatch.setattr(si.platform, "system", lambda: system)
    monkeypatch.setattr(si.platform, "machine", lambda: machine)
    assert si.sdk_platform() == expected


def test_sdk_platform_bad_os(monkeypatch):
    monkeypatch.setattr(si.platform, "system", lambda: "Plan9")
    monkeypatch.setattr(si.platform, "machine", lambda: "x86_64")
    with pytest.raises(ProjectError, match="no OpenMV SDK build for this OS"):
        si.sdk_platform()


def test_sdk_platform_bad_arch(monkeypatch):
    monkeypatch.setattr(si.platform, "system", lambda: "Linux")
    monkeypatch.setattr(si.platform, "machine", lambda: "sparc")
    with pytest.raises(ProjectError, match="no OpenMV SDK build for this CPU"):
        si.sdk_platform()


# --- install_sdk -----------------------------------------------------------

def test_install_sdk_success(tmp_path):
    serve = tmp_path / "serve"
    serve.mkdir()
    archive = _make_bundle(
        "openmv-sdk-%s-%s" % (VERSION, PLAT),
        {"sdk.version": VERSION.encode(), "gcc/bin/arm-none-eabi-gcc": b"ELF"},
    )
    base = _publish(serve, archive)
    dest = tmp_path / "sdk"
    si.install_sdk(VERSION, dest, base_url=base, plat=PLAT)
    # strip-1 removed the top dir: files land directly under dest
    assert (dest / "sdk.version").read_text() == VERSION
    assert (dest / "gcc" / "bin" / "arm-none-eabi-gcc").read_bytes() == b"ELF"


def test_install_sdk_uses_host_platform(tmp_path, monkeypatch):
    # plat omitted -> sdk_platform() is consulted.
    monkeypatch.setattr(si.platform, "system", lambda: "Linux")
    monkeypatch.setattr(si.platform, "machine", lambda: "x86_64")
    serve = tmp_path / "serve"
    serve.mkdir()
    archive = _make_bundle("openmv-sdk-%s-%s" % (VERSION, PLAT), {"sdk.version": VERSION.encode()})
    base = _publish(serve, archive)
    dest = tmp_path / "sdk"
    si.install_sdk(VERSION, dest, base_url=base)
    assert (dest / "sdk.version").read_text() == VERSION


def test_install_sdk_checksum_mismatch(tmp_path):
    serve = tmp_path / "serve"
    serve.mkdir()
    archive = _make_bundle("openmv-sdk-%s-%s" % (VERSION, PLAT), {"sdk.version": VERSION.encode()})
    base = _publish(serve, archive, checksum="00" * 32)  # wrong digest
    with pytest.raises(ProjectError, match="checksum mismatch"):
        si.install_sdk(VERSION, tmp_path / "sdk", base_url=base, plat=PLAT)


def test_install_sdk_download_failure(tmp_path):
    base = (tmp_path / "empty").as_uri()  # nothing published there
    with pytest.raises(ProjectError, match="download failed"):
        si.install_sdk(VERSION, tmp_path / "sdk", base_url=base, plat=PLAT)


def test_install_sdk_missing_checksum(tmp_path):
    serve = tmp_path / "serve"
    serve.mkdir()
    archive = _make_bundle("openmv-sdk-%s-%s" % (VERSION, PLAT), {"sdk.version": VERSION.encode()})
    (serve / NAME).write_bytes(archive)  # archive but no .sha256
    with pytest.raises(ProjectError, match="could not fetch"):
        si.install_sdk(VERSION, tmp_path / "sdk", base_url=serve.as_uri(), plat=PLAT)


def test_install_sdk_windows_1_6_0_shim(tmp_path):
    # Temporary shim: on Windows, a 1.6.0 request fetches the 1.7.0 bundle (which has
    # the gcc driver) and stamps it back as 1.6.0.
    serve = tmp_path / "serve"
    serve.mkdir()
    bundle = "openmv-sdk-1.7.0-windows-x86_64.tar.xz"
    archive = _make_bundle(
        "openmv-sdk-1.7.0-windows-x86_64",
        {"sdk.version": b"1.7.0", "gcc/bin/arm-none-eabi-gcc.exe": b"ELF"},
    )
    (serve / bundle).write_bytes(archive)
    (serve / (bundle + ".sha256")).write_text(
        "%s  %s\n" % (hashlib.sha256(archive).hexdigest(), bundle))
    dest = tmp_path / "openmv-sdk-1.6.0"
    si.install_sdk("1.6.0", dest, base_url=serve.as_uri(), plat="windows-x86_64")
    assert (dest / "gcc" / "bin" / "arm-none-eabi-gcc.exe").read_bytes() == b"ELF"
    assert (dest / "sdk.version").read_text() == "1.6.0"   # stamped to the request


def test_install_sdk_extract_failure(tmp_path):
    serve = tmp_path / "serve"
    serve.mkdir()
    bogus = b"not a real xz archive"
    (serve / NAME).write_bytes(bogus)
    (serve / (NAME + ".sha256")).write_text(
        "%s  %s\n" % (hashlib.sha256(bogus).hexdigest(), NAME))  # checksum matches the junk
    with pytest.raises(ProjectError, match="extraction failed"):
        si.install_sdk(VERSION, tmp_path / "sdk", base_url=serve.as_uri(), plat=PLAT)

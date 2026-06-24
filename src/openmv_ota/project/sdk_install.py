"""Install the prebuilt OpenMV SDK bundle in pure Python -- no make, tar, or wget.

``make sdk`` in the firmware just downloads a per-platform bundle
(``https://download.openmv.io/sdk/openmv-sdk-<version>-<os>-<arch>.tar.xz``),
verifies its sha256, and extracts it to ``~/openmv-sdk-<version>``. Doing the same
here means the toolchain -- which itself *contains* ``make`` -- installs with
nothing but a Python interpreter. That breaks the bootstrap chicken-and-egg (you
needed ``make`` to install the ``make`` the firmware build uses), so a native
Windows build needs only this install plus the bundled tools on ``PATH``.

It also fixes the platform string on Windows: the firmware's shell installer uses
raw ``uname -s`` (``mingw64_nt-...`` under Git Bash), which never matches the
published ``windows-x86_64`` bundle; here the mapping is explicit.
"""

from __future__ import annotations

import hashlib
import platform
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from openmv_ota import __version__

from .errors import ProjectError

SDK_BASE_URL = "https://download.openmv.io/sdk"
# The CDN serving the bundles rejects the default ``Python-urllib`` agent with 403;
# any explicit User-Agent is accepted (the firmware's shell installer uses wget's).
_USER_AGENT = "openmv-ota/%s" % __version__

# CPU name (lowercased ``platform.machine()``) -> the token used in bundle names.
_ARCH = {"x86_64": "x86_64", "amd64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}


def sdk_platform() -> str:
    """The ``<os>-<arch>`` token of a bundle name for this host (e.g.
    ``linux-x86_64``, ``darwin-arm64``, ``windows-x86_64``)."""
    system = platform.system().lower()
    if system not in ("linux", "darwin", "windows"):
        raise ProjectError("no OpenMV SDK build for this OS: %s" % platform.system())
    arch = _ARCH.get(platform.machine().lower())
    if arch is None:
        raise ProjectError("no OpenMV SDK build for this CPU: %s" % platform.machine())
    return "%s-%s" % (system, arch)


def install_sdk(version: str, dest: Path, *, base_url: str = SDK_BASE_URL,
                plat: str | None = None) -> None:
    """Download, sha256-verify, and extract the SDK ``version`` bundle into ``dest``
    (the ``~/openmv-sdk-<version>`` layout the firmware build expects). Raises
    :class:`ProjectError` on any download / checksum / extraction failure."""
    name = "openmv-sdk-%s-%s.tar.xz" % (version, plat or sdk_platform())
    url = "%s/%s" % (base_url, name)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        archive = Path(td) / name
        _download(url, archive)
        expected = _fetch_text(url + ".sha256").split()[0].lower()
        actual = _sha256(archive)
        if actual != expected:
            raise ProjectError(
                "OpenMV SDK checksum mismatch for %s (expected %s, got %s)"
                % (name, expected, actual), exit_code=1)
        _extract_strip1(archive, dest)


def _open(url: str):
    return urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": _USER_AGENT}))


def _download(url: str, dest: Path) -> None:
    try:
        with _open(url) as resp, open(dest, "wb") as f:
            shutil.copyfileobj(resp, f)
    except OSError as e:
        raise ProjectError("OpenMV SDK download failed (%s): %s" % (url, e),
                           exit_code=1) from None


def _fetch_text(url: str) -> str:
    try:
        with _open(url) as resp:
            return resp.read().decode("utf-8")
    except OSError as e:
        raise ProjectError("could not fetch %s: %s" % (url, e), exit_code=1) from None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_strip1(archive: Path, dest: Path) -> None:
    """Extract a ``.tar.xz`` into ``dest``, stripping the bundle's single top-level
    directory (tar ``--strip-components=1``), with tarfile's data filter guarding
    against path traversal."""
    def strip1(member: tarfile.TarInfo, path: str):
        _head, _sep, tail = member.name.partition("/")
        if not tail:
            return None                      # the top-level dir entry itself
        member.name = tail
        # A hard link's target is an archive-root-relative member name (e.g. gcc's
        # ``ld`` -> ``ld.bfd``), so it carries the same top-level dir and must be
        # stripped too, or the data filter can't resolve it. A *sym* link's target
        # is relative to the link itself, so it is left untouched.
        if member.islnk():
            _lhead, _lsep, ltail = member.linkname.partition("/")
            member.linkname = ltail
        return tarfile.data_filter(member, path)

    try:
        with tarfile.open(archive, "r:xz") as tf:
            tf.extractall(dest, filter=strip1)
    except (OSError, tarfile.TarError) as e:
        raise ProjectError("OpenMV SDK extraction failed: %s" % e, exit_code=1) from None

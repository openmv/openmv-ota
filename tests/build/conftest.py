"""Fixtures for the build tests: a pegged project + an app dir."""

from __future__ import annotations

import pytest

NOW = "2026-01-01T00:00:00Z"

DEFAULT_APP = {
    "main.py": "print(1)\n",
    "lib/util.py": "U = 1\n",
    "net.tflite": b"\x00" * 100,
}


@pytest.fixture
def make_project(tmp_path, make_firmware, make_sdk):
    """Create a real pegged project + an app dir. Returns (project_dir, repo, app_dir)."""
    def _make(boards=("OPENMV_N6",), app_files=None, with_mpy_cross=True, extra_config="",
              ota=False):
        from openmv_ota.project import project as proj

        repo = make_firmware(with_mpy_cross=with_mpy_cross)
        home = make_sdk(with_bins=True)
        root = tmp_path / "proj"
        proj.create_project(
            root, firmware=repo, boards=list(boards), product=None, vendor=None,
            sdk_home_override=home, install_sdk=False, allow_dirty=True, force=False, now=NOW,
            ota=ota, ota_keys=2, factory_keys=1,  # small pool: these tests don't exercise keys
        )
        if extra_config:
            cfg = proj.ProjectPaths(root).config
            cfg.write_text(cfg.read_text() + extra_config)
            proj.sync_project(root, firmware=repo, sdk_home_override=home,
                              install_sdk=False, allow_dirty=True, now=NOW)

        app = tmp_path / "appsrc"
        app.mkdir()
        files = DEFAULT_APP if app_files is None else app_files
        for rel, content in files.items():
            f = app / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(content if isinstance(content, (bytes, bytearray)) else content.encode())
        return root, repo, app
    return _make

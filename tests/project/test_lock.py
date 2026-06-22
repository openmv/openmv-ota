"""Tests for the lock container, JSON io, and drift diff."""

from __future__ import annotations

import json

import pytest

from openmv_ota.project import lock as lock_mod
from openmv_ota.project.errors import ProjectError


def _lock(**over):
    base = dict(
        generated_by="openmv-ota 0.0.0",
        generated_at="2026-01-01T00:00:00Z",
        config_digest="sha256:aaa",
        firmware={"version": "5.0.0", "commit": "abc", "dirty": False},
        micropython={"version": "1.28.0"},
        sdk={"version": "1.6.0"},
        toolchain={"vela": {"version": "5.0.0"}},
        submodules=[{"path": "lib/micropython", "commit": "x"}],
        targets={"boards": ["OPENMV_N6"], "resolved": {}},
    )
    base.update(over)
    return lock_mod.Lock(**base)


def test_roundtrip(tmp_path):
    path = tmp_path / lock_mod.LOCK_NAME
    lock_mod.write(path, _lock())
    again = lock_mod.read(path)
    assert again.to_dict() == _lock().to_dict()
    assert again.schema_version == lock_mod.LOCK_SCHEMA_VERSION


def test_ota_roundtrips(tmp_path):
    path = tmp_path / lock_mod.LOCK_NAME
    lock_mod.write(path, _lock(ota=True))
    again = lock_mod.read(path)
    assert again.ota is True
    assert again.to_dict()["ota"] is True


def test_drift_ignores_ota_flag():
    # The ota mode is guarded by config_digest, not compared field-by-field.
    assert lock_mod.drift(_lock(), _lock(ota=True)) == []


def test_read_missing(tmp_path):
    with pytest.raises(ProjectError, match="no openmv-ota.lock.json"):
        lock_mod.read(tmp_path / "openmv-ota.lock.json")


def test_read_corrupt(tmp_path):
    p = tmp_path / lock_mod.LOCK_NAME
    p.write_text("{not json")
    with pytest.raises(ProjectError, match="corrupt"):
        lock_mod.read(p)


def test_read_bad_schema(tmp_path):
    p = tmp_path / lock_mod.LOCK_NAME
    p.write_text(json.dumps({"lock_schema_version": 999}))
    with pytest.raises(ProjectError, match="unsupported schema"):
        lock_mod.read(p)


def test_drift_none():
    assert lock_mod.drift(_lock(), _lock()) == []


def test_drift_ignores_metadata():
    other = _lock(generated_at="2099-12-31T00:00:00Z", generated_by="openmv-ota 9.9")
    assert lock_mod.drift(_lock(), other) == []


def test_drift_reports_changes():
    changed = _lock(firmware={"version": "5.0.1", "commit": "def", "dirty": True})
    changes = lock_mod.drift(_lock(), changed)
    joined = "\n".join(changes)
    assert "firmware.version" in joined
    assert "firmware.commit" in joined
    assert "firmware.dirty" in joined


def test_drift_list_element_change():
    a = _lock(submodules=[{"path": "x", "commit": "1"}])
    b = _lock(submodules=[{"path": "x", "commit": "2"}])
    assert any("submodules[0].commit" in c for c in lock_mod.drift(a, b))


def test_drift_list_length_change():
    a = _lock(submodules=[{"path": "x", "commit": "1"}])
    b = _lock(submodules=[{"path": "x", "commit": "1"}, {"path": "y", "commit": "2"}])
    assert any("entries ->" in c for c in lock_mod.drift(a, b))

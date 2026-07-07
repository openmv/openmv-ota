"""keys/backends.json read/write."""

from __future__ import annotations

import pytest

from openmv_ota.project.backends import backends_path, read_backends, write_backends
from openmv_ota.project.errors import ProjectError


def test_round_trip(tmp_path):
    assert read_backends(tmp_path) == {}                    # absent -> empty
    write_backends(tmp_path, {0x0100: {"backend": "pkcs11", "object_label": "k"},
                              0x0001: {"backend": "aws-kms"}})
    got = read_backends(tmp_path)
    assert got[0x0100]["backend"] == "pkcs11" and got[0x0001]["backend"] == "aws-kms"
    assert '"0x0100"' in backends_path(tmp_path).read_text()   # hex-keyed on disk (committed)


def test_bad_json(tmp_path):
    backends_path(tmp_path).parent.mkdir(parents=True)
    backends_path(tmp_path).write_text("{not json")
    with pytest.raises(ProjectError, match="not valid JSON"):
        read_backends(tmp_path)

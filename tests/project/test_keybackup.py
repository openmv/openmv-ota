"""Tests for the private-key backup codec (openmv_ota.project.keybackup).

The archive carries the PEMs exactly as they sit on disk (already encrypted at rest), so
there is no passphrase here -- what the codec owes us is a loud failure on any damage.
"""

import pytest

from openmv_ota.project import keybackup
from openmv_ota.project.errors import ProjectError

_PEMS = {"ota-0100.pem": b"PEM-A", "factory-0001.pem": b"PEM-B"}


def test_roundtrip():
    blob = keybackup.pack_keys(_PEMS)
    assert blob[:len(keybackup.MAGIC)] == keybackup.MAGIC
    assert keybackup.unpack_keys(blob) == _PEMS


def test_pack_is_deterministic():
    assert keybackup.pack_keys(_PEMS) == keybackup.pack_keys(_PEMS)


def test_empty_refused():
    with pytest.raises(ProjectError, match="no private keys"):
        keybackup.pack_keys({})


def test_bad_magic_and_truncation():
    blob = keybackup.pack_keys(_PEMS)
    with pytest.raises(ProjectError, match="not an openmv-ota key backup"):
        keybackup.unpack_keys(b"XXXXXX" + blob[len(keybackup.MAGIC):])
    with pytest.raises(ProjectError, match="not an openmv-ota key backup"):
        keybackup.unpack_keys(blob[:4])                       # shorter than the header


def test_corruption_fails_loudly():
    blob = bytearray(keybackup.pack_keys(_PEMS))
    blob[-1] ^= 0xFF                                          # damage the payload
    with pytest.raises(ProjectError, match="integrity check failed"):
        keybackup.unpack_keys(bytes(blob))
    blob = bytearray(keybackup.pack_keys(_PEMS))
    blob[len(keybackup.MAGIC)] ^= 0xFF                        # damage the stored digest
    with pytest.raises(ProjectError, match="integrity check failed"):
        keybackup.unpack_keys(bytes(blob))

"""Tests for the encrypted private-key backup codec (openmv_ota.project.keybackup)."""

from __future__ import annotations

import pytest

from openmv_ota.project import keybackup
from openmv_ota.project.errors import ProjectError

_PEMS = {"ota-0100.pem": b"-----BEGIN PRIVATE KEY-----\nAAA\n-----END PRIVATE KEY-----\n",
         "factory-0001.pem": b"-----BEGIN PRIVATE KEY-----\nBBB\n-----END PRIVATE KEY-----\n"}


def test_encrypt_decrypt_roundtrip():
    blob = keybackup.encrypt_keys(_PEMS, "correct horse battery staple")
    assert blob[:len(keybackup.MAGIC)] == keybackup.MAGIC
    assert keybackup.decrypt_keys(blob, "correct horse battery staple") == _PEMS


def test_fixed_salt_is_used():
    salt = b"\x01" * keybackup._SALT_LEN
    blob = keybackup.encrypt_keys(_PEMS, "pw", salt=salt)
    assert blob[len(keybackup.MAGIC):len(keybackup.MAGIC) + keybackup._SALT_LEN] == salt
    assert keybackup.decrypt_keys(blob, "pw") == _PEMS


def test_wrong_passphrase_fails_loudly():
    blob = keybackup.encrypt_keys(_PEMS, "right")
    with pytest.raises(ProjectError, match="wrong passphrase"):
        keybackup.decrypt_keys(blob, "wrong")


def test_encrypt_empty_rejected():
    with pytest.raises(ProjectError, match="no private keys"):
        keybackup.encrypt_keys({}, "pw")


def test_decrypt_bad_magic():
    with pytest.raises(ProjectError, match="not an openmv-ota key backup"):
        keybackup.decrypt_keys(b"XXXX" + b"\x00" * 40, "pw")


def test_decrypt_too_short():
    with pytest.raises(ProjectError, match="not an openmv-ota key backup"):
        keybackup.decrypt_keys(keybackup.MAGIC + b"\x00", "pw")

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


def _archive(entries) -> bytes:
    import hashlib
    import json
    payload = json.dumps(entries).encode()
    return keybackup.MAGIC + hashlib.sha256(payload).digest() + payload


@pytest.mark.parametrize("name", [
    "../../../.bashrc", "/home/user/.ssh/authorized_keys", "sub/ota-0100.pem", "ota-0100.pem/..",
    "..", "", "OTA-0100.PEM", "ota-0100.pem\x00", "payload-keys.bin.bak", ".dev-passphrase",
])
def test_a_backup_names_key_files_and_nothing_else(name, tmp_path):
    """The archive's sha256 is against rot: anyone can make one that passes it. A name that
    is a path would be written wherever it points, with the archive's bytes."""
    import base64
    from openmv_ota.project import project as proj
    blob = _archive({"ota-0100.pem": base64.b64encode(b"ok").decode(),
                     name: base64.b64encode(b"owned").decode()})
    with pytest.raises(ProjectError, match="not a key backup this tool wrote"):
        keybackup.unpack_keys(blob)
    with pytest.raises(ProjectError):
        proj.restore_private_keys(tmp_path / "proj", blob)
    assert not list(tmp_path.rglob("*"))                      # refused before a single write


@pytest.mark.parametrize("entries", [["ota-0100.pem"], {"ota-0100.pem": 7},
                                     {"ota-0100.pem": "not base64!"}])
def test_a_backup_that_is_not_a_table_of_files_is_refused(entries):
    with pytest.raises(ProjectError, match="not a key backup this tool wrote"):
        keybackup.unpack_keys(_archive(entries))


def test_every_name_a_project_backs_up_is_one_restore_takes():
    for name in ("ota-0100.pem", "factory-0001.pem", "root-0.pem", "root-1.pem",
                 "issuer-8000.pem", "desk-9f3a.pem", "payload-keys.bin"):
        assert keybackup.unpack_keys(keybackup.pack_keys({name: b"x"})) == {name: b"x"}

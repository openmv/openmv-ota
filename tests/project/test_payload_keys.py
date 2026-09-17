"""Tests for the project's payload keys: minted once, encrypted at rest, never typed."""

from __future__ import annotations

import pytest

from openmv_ota.ota.payload import KEY_SIZE
from openmv_ota.project import payload_keys
from openmv_ota.project.errors import ProjectError

PASS = "correct horse battery staple"


def test_a_new_project_gets_one_key_per_board(tmp_path):
    payload_keys.mint(tmp_path, ["OPENMV_N6", "OPENMV4"], PASS)
    keys = payload_keys.read(tmp_path, PASS)
    assert sorted(keys) == ["OPENMV4", "OPENMV_N6"]
    assert list(keys["OPENMV4"]) == [1]
    assert len(keys["OPENMV4"][1]) == KEY_SIZE
    # per board target, so a dump of one board type is not a dump of the other
    assert keys["OPENMV4"][1] != keys["OPENMV_N6"][1]


def test_the_key_is_not_on_disk_in_the_clear(tmp_path):
    payload_keys.mint(tmp_path, ["OPENMV4"], PASS)
    key = payload_keys.read(tmp_path, PASS)["OPENMV4"][1]
    blob = payload_keys.path_for(tmp_path).read_bytes()
    assert key not in blob
    assert key.hex().encode() not in blob


def test_the_wrong_passphrase_does_not_open_it(tmp_path):
    payload_keys.mint(tmp_path, ["OPENMV4"], PASS)
    with pytest.raises(ProjectError, match="wrong passphrase, or the file has been altered"):
        payload_keys.read(tmp_path, "not the passphrase")


def test_an_edited_file_fails_before_anything_is_decrypted(tmp_path):
    payload_keys.mint(tmp_path, ["OPENMV4"], PASS)
    blob = bytearray(payload_keys.path_for(tmp_path).read_bytes())
    blob[-1] ^= 0xFF
    payload_keys.path_for(tmp_path).write_bytes(bytes(blob))
    with pytest.raises(ProjectError, match="wrong passphrase, or the file has been altered"):
        payload_keys.read(tmp_path, PASS)


def test_something_else_entirely_is_not_read_as_keys(tmp_path):
    payload_keys.path_for(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    payload_keys.path_for(tmp_path).write_bytes(b"just a file")
    with pytest.raises(ProjectError, match="not an openmv-ota payload key file"):
        payload_keys.read(tmp_path, PASS)


def test_a_missing_file_says_what_it_costs_and_how_to_get_it_back(tmp_path):
    with pytest.raises(ProjectError, match="cannot decrypt anything you publish"):
        payload_keys.read(tmp_path, PASS)


def test_a_board_added_later_gets_a_key_without_a_ceremony(tmp_path):
    payload_keys.mint(tmp_path, ["OPENMV4"], PASS)
    assert payload_keys.ensure_boards(tmp_path, ["OPENMV4", "OPENMV_N6"], PASS) == ["OPENMV_N6"]
    assert sorted(payload_keys.read(tmp_path, PASS)) == ["OPENMV4", "OPENMV_N6"]
    # ...and running it again mints nothing and rewrites nothing
    before = payload_keys.path_for(tmp_path).read_bytes()
    assert payload_keys.ensure_boards(tmp_path, ["OPENMV4", "OPENMV_N6"], PASS) == []
    assert payload_keys.path_for(tmp_path).read_bytes() == before


def test_a_rotation_keeps_the_old_key(tmp_path):
    """Firmware is the only way a new key reaches a camera, so the fleet has to keep
    being served by the old one until it has moved."""
    payload_keys.mint(tmp_path, ["OPENMV4"], PASS)
    first = payload_keys.read(tmp_path, PASS)["OPENMV4"][1]
    assert payload_keys.rotate(tmp_path, "OPENMV4", PASS) == 2
    keys = payload_keys.read(tmp_path, PASS)["OPENMV4"]
    assert keys[1] == first and keys[2] != first


def test_rotating_a_board_that_is_not_in_the_project_is_a_mistake(tmp_path):
    payload_keys.mint(tmp_path, ["OPENMV4"], PASS)
    with pytest.raises(ProjectError, match="not a board in this project"):
        payload_keys.rotate(tmp_path, "OPENMV_N6", PASS)


def test_retiring_the_old_key_is_a_separate_later_decision(tmp_path):
    payload_keys.mint(tmp_path, ["OPENMV4"], PASS)
    payload_keys.rotate(tmp_path, "OPENMV4", PASS)
    payload_keys.retire(tmp_path, "OPENMV4", 1, PASS)
    assert list(payload_keys.read(tmp_path, PASS)["OPENMV4"]) == [2]


def test_the_last_key_cannot_be_retired(tmp_path):
    payload_keys.mint(tmp_path, ["OPENMV4"], PASS)
    with pytest.raises(ProjectError, match="Rotate first"):
        payload_keys.retire(tmp_path, "OPENMV4", 1, PASS)


def test_retiring_a_key_that_is_not_there_says_so(tmp_path):
    payload_keys.mint(tmp_path, ["OPENMV4"], PASS)
    with pytest.raises(ProjectError, match="has no payload key 7"):
        payload_keys.retire(tmp_path, "OPENMV4", 7, PASS)


def test_a_new_ota_project_arrives_with_payload_keys(tmp_path, make_firmware, make_sdk):
    """The whole point of the feature is that nobody decides about it: an OTA project
    has a board key per board the moment it exists, and it is in the key backup --
    losing it is losing the fleet's updates."""
    from openmv_ota.project import project as proj

    root = tmp_path / "proj"
    proj.create_project(root, firmware=make_firmware(), boards=["OPENMV_N6", "OPENMV_AE3"],
                        product=None, vendor=None, sdk_home_override=make_sdk(),
                        install_sdk=False, allow_dirty=True, force=False,
                        now="2026-01-01T00:00:00Z", dev=True, ota=True)
    private = root / "keys" / "private"
    dev_pass = (root / "keys" / ".dev-passphrase").read_text().strip()
    assert sorted(payload_keys.read(private, dev_pass)) == ["OPENMV_AE3", "OPENMV_N6"]

    from openmv_ota.project import keybackup
    (root / "keys" / ".dev-passphrase").unlink()      # a backup is refused for a dev project
    archive = proj.backup_private_keys(root)
    assert payload_keys.FILE_NAME in keybackup.unpack_keys(archive.read_bytes())

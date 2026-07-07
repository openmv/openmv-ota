"""Tests for the OTA key lifecycle (status / rotate / revoke / unrevoke)."""

from __future__ import annotations

import pytest

from openmv_ota.project import config as cfg
from openmv_ota.project import keys as keys_mod
from openmv_ota.project import project as proj
from openmv_ota.project.errors import ProjectError

NOW = "2026-01-01T00:00:00Z"


def _ota_project(tmp_path, make_firmware, make_sdk, ota_keys=4, factory_keys=2):
    repo = make_firmware()
    root = tmp_path / "proj"
    proj.create_project(
        root, firmware=repo, boards=["OPENMV_N6"], product=None, vendor=None,
        sdk_home_override=make_sdk(), install_sdk=False, allow_dirty=True, force=False, now=NOW,
        ota=True, ota_keys=ota_keys, factory_keys=factory_keys, dev=True)
    return root


def _signing_id(root):
    return cfg.load_config(proj.ProjectPaths(root).config).signing_key_id


def _make_plaintext(root, pattern="ota-*.pem"):
    """Overwrite one private PEM with a plaintext one, simulating a pre-encryption project."""
    from cryptography.hazmat.primitives import serialization

    from openmv_ota.ota import algorithm_for
    from openmv_ota.ota.algorithms import ES256
    from openmv_ota.ota.keys import generate_private_key
    target = sorted((root / "keys" / "private").glob(pattern))[0]
    key = generate_private_key(algorithm_for(ES256))
    target.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                         serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    return target


def test_encrypt_private_keys_migrates_and_is_idempotent(tmp_path, make_firmware, make_sdk):
    from openmv_ota.ota.errors import OtaError
    from openmv_ota.ota.keys import load_private_key_pem
    root = _ota_project(tmp_path, make_firmware, make_sdk)
    target = _make_plaintext(root)
    assert target.name in keys_mod.encrypt_private_keys(root, "newpw")   # migrated
    with pytest.raises(OtaError):
        load_private_key_pem(target.read_bytes(), None)                  # no longer plaintext
    assert load_private_key_pem(target.read_bytes(), "newpw") is not None
    assert keys_mod.encrypt_private_keys(root, "newpw") == []            # idempotent (all encrypted)


def test_keys_encrypt_cli(tmp_path, make_firmware, make_sdk, capsys):
    from openmv_ota.cli import main
    from openmv_ota.project.passphrase import dev_passphrase_path
    root = _ota_project(tmp_path, make_firmware, make_sdk)
    _make_plaintext(root)
    assert main(["project", "keys", "encrypt", str(root)]) == 2          # needs a passphrase source
    assert "--key-passphrase-file" in capsys.readouterr().err
    assert main(["project", "keys", "encrypt", "--dev", str(root)]) == 0  # dev path caches a passphrase
    assert "Encrypted 1" in capsys.readouterr().out and dev_passphrase_path(root).exists()
    _make_plaintext(root, "factory-*.pem")
    pf = tmp_path / "pf"
    pf.write_text("realpw")
    assert main(["project", "keys", "encrypt", "--key-passphrase-file", str(pf), str(root)]) == 0
    assert "Encrypted 1" in capsys.readouterr().out
    assert main(["project", "keys", "encrypt", "--dev", str(root)]) == 0  # nothing plaintext left
    assert "No plaintext keys" in capsys.readouterr().out


def test_status_fresh(tmp_path, make_firmware, make_sdk):
    root = _ota_project(tmp_path, make_firmware, make_sdk, ota_keys=4, factory_keys=2)
    st = keys_mod.key_status(root)
    assert st.signing_key_id == 0x0100 and not st.signer_revoked
    assert st.retired == 0 and st.remaining == 3 and st.revoked == 0
    assert len(st.ota_ids) == 4 and len(st.factory_ids) == 2
    assert st.private_present == 6 and st.private_total == 6  # all PEMs on the signer


def test_rotate_advances_then_exhausts(tmp_path, make_firmware, make_sdk):
    root = _ota_project(tmp_path, make_firmware, make_sdk, ota_keys=3, factory_keys=1)
    assert keys_mod.rotate_signing_key(root)[:2] == (0x0100, 0x0101)
    assert _signing_id(root) == 0x0101
    assert keys_mod.rotate_signing_key(root)[:2] == (0x0101, 0x0102)
    with pytest.raises(ProjectError, match="no more OTA keys"):
        keys_mod.rotate_signing_key(root)


def test_rotate_skips_revoked(tmp_path, make_firmware, make_sdk):
    root = _ota_project(tmp_path, make_firmware, make_sdk, ota_keys=4, factory_keys=1)
    keys_mod.revoke_key(root, 0x0101)
    assert keys_mod.rotate_signing_key(root)[:2] == (0x0100, 0x0102)  # skipped 0x0101


def test_revoke_and_unrevoke_roundtrip(tmp_path, make_firmware, make_sdk):
    root = _ota_project(tmp_path, make_firmware, make_sdk, ota_keys=4, factory_keys=1)
    key, changed, is_signer = keys_mod.revoke_key(root, 0x0102)
    assert changed and not is_signer and key.role == "ota"
    assert keys_mod.key_status(root).revoked == 1
    assert keys_mod.revoke_key(root, 0x0102)[1] is False  # idempotent
    assert keys_mod.unrevoke_key(root, 0x0102)[1] is True
    assert keys_mod.key_status(root).revoked == 0
    assert keys_mod.unrevoke_key(root, 0x0102)[1] is False  # already not revoked


def test_revoke_current_signer_flags_not_advances(tmp_path, make_firmware, make_sdk):
    # (B) revoke only marks; the signer stays put (build refuses it) until you rotate.
    root = _ota_project(tmp_path, make_firmware, make_sdk, ota_keys=4, factory_keys=1)
    _key, changed, is_signer = keys_mod.revoke_key(root, 0x0100)
    assert changed and is_signer
    st = keys_mod.key_status(root)
    assert st.signer_revoked and st.signing_key_id == 0x0100


def test_revoke_unknown_key(tmp_path, make_firmware, make_sdk):
    root = _ota_project(tmp_path, make_firmware, make_sdk)
    with pytest.raises(ProjectError, match="no key with id"):
        keys_mod.revoke_key(root, 0x9999)


def test_keys_require_ota_project(tmp_path, make_firmware, make_sdk):
    repo = make_firmware()
    root = tmp_path / "plain"
    proj.create_project(
        root, firmware=repo, boards=["OPENMV_N6"], product=None, vendor=None,
        sdk_home_override=make_sdk(), install_sdk=False, allow_dirty=True, force=False, now=NOW)
    with pytest.raises(ProjectError, match="not an OTA project"):
        keys_mod.key_status(root)
    with pytest.raises(ProjectError, match="not an OTA project"):
        keys_mod.rotate_signing_key(root)


def test_status_missing_trusted_keys(tmp_path, make_firmware, make_sdk):
    root = _ota_project(tmp_path, make_firmware, make_sdk)
    proj.ProjectPaths(root).trusted_keys.unlink()
    with pytest.raises(ProjectError, match="no trusted_keys.json"):
        keys_mod.key_status(root)


def test_signing_key_not_in_pool(tmp_path, make_firmware, make_sdk):
    root = _ota_project(tmp_path, make_firmware, make_sdk)
    cfg.set_signing_key_id(proj.ProjectPaths(root).config, 0x0001)  # a factory id
    with pytest.raises(ProjectError, match="not an OTA key"):
        keys_mod.key_status(root)


def test_rotate_warns_missing_next_pem(tmp_path, make_firmware, make_sdk):
    root = _ota_project(tmp_path, make_firmware, make_sdk, ota_keys=4, factory_keys=1)
    (proj.ProjectPaths(root).private_keys_dir / "ota-0101.pem").unlink()
    _old, new, warnings = keys_mod.rotate_signing_key(root)
    assert new == 0x0101 and any("not on this machine" in w for w in warnings)


def test_set_signing_key_id_missing_line(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[product]\nname = 'x'\n")
    with pytest.raises(ProjectError, match="could not find signing_key_id"):
        cfg.set_signing_key_id(p, 5)

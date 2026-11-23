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


def test_status_fresh(tmp_path, make_firmware, make_sdk):
    root = _ota_project(tmp_path, make_firmware, make_sdk, ota_keys=4, factory_keys=2)
    st = keys_mod.key_status(root)
    assert st.signing_key_id == 0x0100 and not st.signer_revoked
    assert st.used == 0 and st.remaining == 3 and st.revoked == 0
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


class _FakeProvisioner:
    """Mints real local keypairs so the returned public points are valid, but hands back an
    external-style backend record (no PEM written)."""

    def __init__(self, tag="aws-kms"):
        self.tag = tag
        self.calls: list[tuple[int, str]] = []

    def provision(self, key_id, role, alg):
        from openmv_ota.ota.keys import generate_private_key, public_point_hex
        self.calls.append((key_id, role))
        pub = generate_private_key(alg).public_key()
        return public_point_hex(pub), {"backend": self.tag, "uri": "res:%s-%04x" % (role, key_id)}


def test_backend_summary_defaults_and_after_configure(tmp_path, make_firmware, make_sdk):
    root = _ota_project(tmp_path, make_firmware, make_sdk, ota_keys=2, factory_keys=1)
    rows = keys_mod.backend_summary(root)
    assert [r[2] for r in rows] == ["encrypted-pem"] * 3          # every key is a local PEM
    ota_id = _signing_id(root)
    keys_mod.set_backend(root, ota_id, {"backend": "aws-kms", "uri": "arn:x"})
    after = {kid: backend for kid, _role, backend in keys_mod.backend_summary(root)}
    assert after[ota_id] == "aws-kms" and after[0x0001] == "encrypted-pem"


def test_set_backend_unknown_key(tmp_path, make_firmware, make_sdk):
    root = _ota_project(tmp_path, make_firmware, make_sdk)
    with pytest.raises(ProjectError, match="no key with id"):
        keys_mod.set_backend(root, 0x9999, {"backend": "pkcs11"})


def test_provision_backend_rekeys(tmp_path, make_firmware, make_sdk):
    from openmv_ota.ota import read_trusted_keys
    from openmv_ota.project.backends import read_backends
    root = _ota_project(tmp_path, make_firmware, make_sdk, ota_keys=4, factory_keys=2)
    fake = _FakeProvisioner()
    signing = keys_mod.provision_backend(root, fake, n_factory=1, n_ota=2)

    trusted = read_trusted_keys(proj.ProjectPaths(root).trusted_keys)
    assert [k.role for k in trusted] == ["factory", "ota", "ota"]     # the whole set replaced
    assert signing == _signing_id(root)                              # config advanced to new signer
    records = read_backends(root)
    assert {k.key_id for k in trusted} == set(records)               # every key has a backend record
    assert all(r["backend"] == "aws-kms" for r in records.values())
    assert len(fake.calls) == 4 and fake.calls[-1] == (0x0200, "issuer")  # the issuer moved too
    from openmv_ota.project import issuer as issuer_mod
    issuer = issuer_mod.read(root)
    assert issuer.backend["backend"] == "aws-kms" and not list(
        proj.ProjectPaths(root).private_keys_dir.glob("issuer-*.pem"))  # no PEM: it lives there


def test_provision_backend_needs_the_root_key(tmp_path, make_firmware, make_sdk):
    """The root certifies the new keys; without its PEM there is nothing to certify with."""
    root = _ota_project(tmp_path, make_firmware, make_sdk, ota_keys=2, factory_keys=1)
    (proj.ProjectPaths(root).private_keys_dir / "root-0.pem").unlink()
    with pytest.raises(ProjectError, match="no root-0.pem in keys/private"):
        keys_mod.provision_backend(root, _FakeProvisioner(), n_factory=1, n_ota=1)


def test_keys_backend_show_and_configure_cli(tmp_path, make_firmware, make_sdk, capsys):
    from openmv_ota.cli import main
    from openmv_ota.project.backends import read_backends
    root = _ota_project(tmp_path, make_firmware, make_sdk, ota_keys=2, factory_keys=1)
    ota_id = _signing_id(root)
    assert main(["project", "keys", "backend", "configure", "0x%04x" % ota_id, str(root),
                 "--backend", "aws-kms", "--set", "uri=arn:aws:kms:x"]) == 0
    assert read_backends(root)[ota_id] == {"backend": "aws-kms", "uri": "arn:aws:kms:x"}
    assert main(["project", "keys", "backend", "show", str(root)]) == 0
    out = capsys.readouterr().out
    assert "aws-kms" in out and ("0x%04x" % ota_id) in out


def test_keys_backend_configure_bad_set(tmp_path, make_firmware, make_sdk):
    from openmv_ota.cli import main
    root = _ota_project(tmp_path, make_firmware, make_sdk)
    assert main(["project", "keys", "backend", "configure", "0x0100", str(root),
                 "--backend", "pkcs11", "--set", "noequals"]) == 2


def test_keys_backend_provision_cli(tmp_path, make_firmware, make_sdk, capsys, monkeypatch):
    from openmv_ota.cli import main
    from openmv_ota.ota import signer
    from openmv_ota.project.backends import read_backends
    root = _ota_project(tmp_path, make_firmware, make_sdk, ota_keys=4, factory_keys=2)
    fake = _FakeProvisioner(tag="pkcs11")
    captured = {}

    def _fake_build(record):
        captured["record"] = record
        return fake

    monkeypatch.setattr(signer, "build_provisioner", _fake_build)
    assert main(["project", "keys", "backend", "provision", "--backend", "pkcs11",
                 "--set", "pkcs11_module=/usr/lib/softhsm.so",
                 "--ota-keys", "2", "--factory-keys", "1", str(root)]) == 0
    assert captured["record"] == {"backend": "pkcs11", "pkcs11_module": "/usr/lib/softhsm.so"}
    assert len(read_backends(root)) == 3
    assert "no private key on disk" in capsys.readouterr().out


def test_keys_backend_provision_error_not_ota(tmp_path, make_firmware, make_sdk, monkeypatch):
    from openmv_ota.cli import main
    from openmv_ota.ota import signer
    monkeypatch.setattr(signer, "build_provisioner", lambda record: _FakeProvisioner())
    repo = make_firmware()
    root = tmp_path / "plain"
    proj.create_project(root, firmware=repo, boards=["OPENMV_N6"], product=None, vendor=None,
                        sdk_home_override=make_sdk(), install_sdk=False, allow_dirty=True,
                        force=False, now=NOW, ota=False)
    assert main(["project", "keys", "backend", "provision", "--backend", "aws-kms", str(root)]) == 2


def test_keys_backend_show_not_ota(tmp_path, make_firmware, make_sdk):
    from openmv_ota.cli import main
    repo = make_firmware()
    root = tmp_path / "plain"
    proj.create_project(root, firmware=repo, boards=["OPENMV_N6"], product=None, vendor=None,
                        sdk_home_override=make_sdk(), install_sdk=False, allow_dirty=True,
                        force=False, now=NOW, ota=False)
    assert main(["project", "keys", "backend", "show", str(root)]) == 2


# --- the provisioning prompt (double entry) -----------------------------------

def test_prompt_new_passphrase_match_and_mismatch():
    from openmv_ota.project.errors import ProjectError
    from openmv_ota.project.passphrase import prompt_new_passphrase

    answers = iter(["s3cret", "s3cret"])
    assert prompt_new_passphrase(_getpass=lambda _p: next(answers)) == "s3cret"

    answers = iter(["s3cret", "typo"])
    with pytest.raises(ProjectError, match="do not match"):
        prompt_new_passphrase(_getpass=lambda _p: next(answers))

    with pytest.raises(ProjectError, match="empty passphrase"):
        prompt_new_passphrase(_getpass=lambda _p: "  ")


# --- retirement: what makes a revocation reach the fleet -------------------------------------

def _trusted(root):
    from openmv_ota.ota import read_trusted_keys
    return {k.key_id: k for k in read_trusted_keys(proj.ProjectPaths(root).trusted_keys)}


def test_retire_moves_the_live_keys_to_the_next_epoch(tmp_path, make_firmware, make_sdk):
    from openmv_ota.ota import certificate
    from openmv_ota.ota.keys import read_root_keys, root_keys_map
    root = _ota_project(tmp_path, make_firmware, make_sdk, ota_keys=3, factory_keys=1)
    st = keys_mod.key_status(root)
    assert st.epoch == 0 and st.unretired == 0
    keys_mod.revoke_key(root, 0x0101)
    assert keys_mod.key_status(root).unretired == 1            # revoked, and cameras still take it
    before = _trusted(root)

    assert keys_mod.retire_revoked(root) == (1, [0x0101])
    after = _trusted(root)
    roots = root_keys_map(read_root_keys(proj.ProjectPaths(root).root_keys))
    for kid, k in after.items():
        c = certificate.verify(bytes.fromhex(k.cert), roots)   # still a certificate root 0 signed
        assert c.epoch == (0 if kid == 0x0101 else 1)          # the revoked key is left behind
        assert c.pubkey == bytes.fromhex(before[kid].pubkey)   # same keys: only the epoch moved
    assert after[0x0101].cert == before[0x0101].cert
    st = keys_mod.key_status(root)
    assert st.epoch == 1 and st.unretired == 0 and st.revoked == 1

    with pytest.raises(ProjectError, match="no revoked key to retire"):
        keys_mod.retire_revoked(root)                          # nothing is waiting now
    with pytest.raises(ProjectError, match="was retired"):
        keys_mod.unrevoke_key(root, 0x0101)                    # and nothing brings it back
    keys_mod.revoke_key(root, 0x0001)                          # a factory key, later
    assert keys_mod.retire_revoked(root) == (2, [0x0001])
    assert keys_mod.key_epoch(_trusted(root)[0x0101]) == 0


def test_retire_refuses_with_nothing_left_to_sign_and_without_the_root(tmp_path, make_firmware,
                                                                       make_sdk):
    root = _ota_project(tmp_path, make_firmware, make_sdk, ota_keys=2, factory_keys=1)
    with pytest.raises(ProjectError, match="no revoked key to retire"):
        keys_mod.retire_revoked(root)
    keys_mod.revoke_key(root, 0x0100)
    (proj.ProjectPaths(root).private_keys_dir / "root-0.pem").rename(tmp_path / "root-0.pem")
    with pytest.raises(ProjectError, match="no root-0.pem in keys/private"):
        keys_mod.retire_revoked(root)
    (tmp_path / "root-0.pem").rename(proj.ProjectPaths(root).private_keys_dir / "root-0.pem")
    keys_mod.revoke_key(root, 0x0101)
    with pytest.raises(ProjectError, match="every OTA key is revoked"):
        keys_mod.retire_revoked(root)


def test_provision_backend_retires_the_keys_it_replaces(tmp_path, make_firmware, make_sdk):
    """The old keys leave trusted_keys.json, so nothing could retire them afterwards, while
    their certificates and PEMs live on: the new set goes one epoch up. That also covers a
    key that was revoked and not yet retired, which used to vanish from the set unrefused."""
    root = _ota_project(tmp_path, make_firmware, make_sdk, ota_keys=2, factory_keys=1)
    keys_mod.provision_backend(root, _FakeProvisioner(), n_factory=1, n_ota=2)
    assert {keys_mod.key_epoch(k) for k in _trusted(root).values()} == {1}
    keys_mod.revoke_key(root, 0x0100)                       # revoked, never retired...
    keys_mod.provision_backend(root, _FakeProvisioner(), n_factory=1, n_ota=2)
    assert {keys_mod.key_epoch(k) for k in _trusted(root).values()} == {2}   # ...and still refused


def test_a_pin_is_refused_in_a_backend_record(tmp_path, make_firmware, make_sdk, monkeypatch):
    from openmv_ota.ota import signer_pkcs11
    from openmv_ota.ota.errors import OtaError
    root = _ota_project(tmp_path, make_firmware, make_sdk)
    with pytest.raises(ProjectError, match="OPENMV_OTA_PKCS11_PIN"):
        keys_mod.set_backend(root, 0x0100, {"backend": "pkcs11", "pin": "1234"})
    with pytest.raises(OtaError, match="does not go in a backend record"):
        signer_pkcs11.token_pin({"pkcs11_module": "m.so", "pin": "1234"})
    monkeypatch.delenv("OPENMV_OTA_PKCS11_PIN", raising=False)
    assert signer_pkcs11.token_pin({"pkcs11_module": "m.so"}) is None
    monkeypatch.setenv("OPENMV_OTA_PKCS11_PIN", "4321")
    assert signer_pkcs11.token_pin({"pkcs11_module": "m.so"}) == "4321"


def test_code_to_load_is_refused_in_a_backend_record(tmp_path, make_firmware, make_sdk,
                                                     monkeypatch):
    """`keys/backends.json` comes with the project. A PKCS#11 library or a signer factory
    named there is code that whoever made the project has the signing machine run."""
    from openmv_ota.ota import signer_pkcs11
    from openmv_ota.ota.errors import OtaError
    root = _ota_project(tmp_path, make_firmware, make_sdk)
    for record, env in (({"backend": "pkcs11", "pkcs11_module": "/tmp/evil.so"},
                         "OPENMV_OTA_PKCS11_MODULE"),
                        ({"backend": "custom", "factory": "evil:run"},
                         "OPENMV_OTA_SIGNER_FACTORY")):
        with pytest.raises(ProjectError, match=env):
            keys_mod.set_backend(root, 0x0100, record)
    with pytest.raises(OtaError, match="does not go in a backend record"):
        signer_pkcs11.token_module({"pkcs11_module": "/tmp/evil.so"})
    monkeypatch.delenv("OPENMV_OTA_PKCS11_MODULE", raising=False)
    with pytest.raises(OtaError, match="set OPENMV_OTA_PKCS11_MODULE"):
        signer_pkcs11.token_module({"token_label": "openmv"})
    monkeypatch.setenv("OPENMV_OTA_PKCS11_MODULE", "/usr/lib/softhsm/libsofthsm2.so")
    assert signer_pkcs11.token_module({}) == "/usr/lib/softhsm/libsofthsm2.so"


def test_keys_retire_cli(tmp_path, make_firmware, make_sdk, capsys):
    from openmv_ota.cli import main
    root = _ota_project(tmp_path, make_firmware, make_sdk, ota_keys=3, factory_keys=1)
    assert main(["project", "keys", "retire", str(root)]) == 2
    assert "no revoked key to retire" in capsys.readouterr().err
    assert main(["project", "keys", "revoke", "0x0102", str(root)]) == 0
    assert "Cameras still accept it until" in capsys.readouterr().out
    assert main(["project", "keys", "status", str(root)]) == 0
    assert "epoch:        0  (1 revoked key(s) cameras still accept" in capsys.readouterr().out
    assert main(["project", "keys", "retire", str(root)]) == 0
    assert "Retired 0x0102: releases are signed at epoch 1 from now on." in capsys.readouterr().out
    assert main(["project", "keys", "status", str(root)]) == 0
    assert "epoch:        1\n" in capsys.readouterr().out
    assert main(["project", "keys", "unrevoke", "0x0102", str(root)]) == 2
    assert "was retired" in capsys.readouterr().err


# --- keys add: the pool grows -----------------------------------------------------------------

def test_add_keys_follow_their_roles_and_carry_the_epoch(tmp_path, make_firmware, make_sdk):
    from openmv_ota.ota import certificate
    from openmv_ota.ota.keys import load_private_key_pem, public_point_hex, read_root_keys, root_keys_map
    from openmv_ota.project import passphrase as passphrase_mod
    root = _ota_project(tmp_path, make_firmware, make_sdk, ota_keys=2, factory_keys=1)
    keys_mod.revoke_key(root, 0x0101)
    keys_mod.retire_revoked(root)                               # the project signs at epoch 1 now
    assert keys_mod.add_keys(root, n_ota=2, n_factory=1) == [0x0002, 0x0102, 0x0103]
    trusted = _trusted(root)
    assert list(trusted) == [0x0001, 0x0002, 0x0100, 0x0101, 0x0102, 0x0103]
    assert (trusted[0x0002].role, trusted[0x0103].role) == ("factory", "ota")
    paths = proj.ProjectPaths(root)
    roots = root_keys_map(read_root_keys(paths.root_keys))
    phrase, _ = passphrase_mod.resolve_passphrase(root)
    for key_id, name in ((0x0002, "factory-0002.pem"), (0x0102, "ota-0102.pem"),
                         (0x0103, "ota-0103.pem")):
        cert = certificate.verify(bytes.fromhex(trusted[key_id].cert), roots,
                                  kinds=(certificate.KIND_OTA, certificate.KIND_FACTORY))
        assert cert.epoch == 1
        priv = load_private_key_pem((paths.private_keys_dir / name).read_bytes(), phrase)
        assert public_point_hex(priv.public_key()) == trusted[key_id].pubkey
    assert _signing_id(root) == 0x0100                          # the signer stayed put
    assert keys_mod.rotate_signing_key(root)[:2] == (0x0100, 0x0102)   # 0x0101 is revoked
    st = keys_mod.key_status(root)
    assert st.remaining == 1 and st.private_present == st.private_total == 6
    assert keys_mod.add_keys(root, n_ota=1) == [0x0104]         # an id is never reused


def test_add_keys_in_a_backend(tmp_path, make_firmware, make_sdk):
    from openmv_ota.project.backends import read_backends
    root = _ota_project(tmp_path, make_firmware, make_sdk, ota_keys=1, factory_keys=1)
    fake = _FakeProvisioner()
    assert keys_mod.add_keys(root, n_ota=1, provisioner=fake) == [0x0101]
    assert fake.calls == [(0x0101, "ota")]
    assert read_backends(root) == {0x0101: {"backend": "aws-kms", "uri": "res:ota-0101"}}
    assert not (proj.ProjectPaths(root).private_keys_dir / "ota-0101.pem").exists()
    assert dict(((k, b) for k, _r, b in keys_mod.backend_summary(root))) == {
        0x0001: "encrypted-pem", 0x0100: "encrypted-pem", 0x0101: "aws-kms"}


def test_add_keys_refusals(tmp_path, make_firmware, make_sdk):
    root = _ota_project(tmp_path, make_firmware, make_sdk, ota_keys=1, factory_keys=1)
    before = proj.ProjectPaths(root).trusted_keys.read_text()
    with pytest.raises(ProjectError, match="nothing to add"):
        keys_mod.add_keys(root)
    with pytest.raises(ProjectError, match="nothing to add"):
        keys_mod.add_keys(root, n_ota=-1, n_factory=1)
    with pytest.raises(ProjectError, match="no room for 255 more factory keys"):
        keys_mod.add_keys(root, n_factory=255)                  # 0x0002 + 255 runs into the OTA ids
    (proj.ProjectPaths(root).private_keys_dir / "root-0.pem").unlink()
    with pytest.raises(ProjectError, match="no root-0.pem"):
        keys_mod.add_keys(root, n_ota=1)
    assert proj.ProjectPaths(root).trusted_keys.read_text() == before


def test_keys_add_cli(tmp_path, make_firmware, make_sdk, capsys, monkeypatch):
    from openmv_ota.cli import main
    from openmv_ota.ota import signer
    root = _ota_project(tmp_path, make_firmware, make_sdk, ota_keys=1, factory_keys=1)
    assert main(["project", "keys", "add", str(root)]) == 2
    assert "nothing to add" in capsys.readouterr().err
    assert main(["project", "keys", "add", "--set", "a=b", "--ota-keys", "1", str(root)]) == 2
    assert "give --backend with it" in capsys.readouterr().err
    assert main(["project", "keys", "add", "--ota-keys", "2", str(root)]) == 0
    out = capsys.readouterr().out
    assert "Added 0x0101, 0x0102 (encrypted PEMs in keys/private)." in out
    assert "Commit keys/trusted_keys.json. Back the new private keys up" in out
    captured = {}

    def _fake_build(record):
        captured["record"] = record
        return _FakeProvisioner(tag="gcp-kms")

    monkeypatch.setattr(signer, "build_provisioner", _fake_build)
    assert main(["project", "keys", "add", "--factory-keys", "1", "--backend", "gcp-kms",
                 "--set", "key_ring=r", str(root)]) == 0
    assert captured["record"] == {"backend": "gcp-kms", "key_ring": "r"}
    out = capsys.readouterr().out
    assert "Added 0x0002 (in the gcp-kms backend)." in out
    assert "Commit keys/trusted_keys.json and keys/backends.json." in out
    assert main(["project", "keys", "status", str(root)]) == 0
    assert "2 remaining" in capsys.readouterr().out

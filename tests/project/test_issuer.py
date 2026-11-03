"""The issuer key: root 0 certifies it once; it certifies every desk key after that."""

from __future__ import annotations

import json

import pytest

from openmv_ota.ota import certificate as cert_mod
from openmv_ota.ota.keys import (
    provision_roots,
    root_keys_map,
    root_pem_name,
    write_root_keys,
)
from openmv_ota.project import desk_keys as dk
from openmv_ota.project import issuer
from openmv_ota.project.errors import ProjectError

PASS = "correct horse battery staple"


def roots_for(root, passphrase=PASS):
    """A project's roots on disk (public set + root PEMs), as `project new` leaves them.
    Returns the ``{root_id: pubkey}`` map a verifier takes."""
    roots, pems = provision_roots(passphrase)
    private = root / "keys" / "private"
    private.mkdir(parents=True, exist_ok=True)
    write_root_keys(root / "keys" / "root_keys.json", roots)
    for root_id, pem in pems.items():
        (private / root_pem_name(root_id)).write_bytes(pem)
    return root_keys_map(roots)


def test_a_project_starts_with_no_issuer(tmp_path):
    assert issuer.read(tmp_path) is None
    with pytest.raises(ProjectError, match="no issuer key"):
        issuer.certify_desk(tmp_path, "04" + "00" * 64)


def test_create_needs_the_root_once_and_writes_a_root_signed_certificate(tmp_path):
    with pytest.raises(ProjectError, match="no root-0.pem"):
        issuer.create(tmp_path, PASS)
    roots = roots_for(tmp_path)
    entry = issuer.create(tmp_path, PASS)
    assert issuer.read(tmp_path) == entry
    cert = cert_mod.verify(bytes.fromhex(entry.cert), roots, kinds=(cert_mod.KIND_ISSUER,))
    assert cert.pubkey.hex() == entry.pubkey and cert.root_id == 0 and cert.epoch == 0
    assert entry.key_id == cert.key_id == cert_mod.key_id_of(cert.pubkey)
    pem = (tmp_path / "keys" / "private" / ("issuer-%04x.pem" % entry.key_id)).read_bytes()
    assert b"ENCRYPTED" in pem
    assert json.loads(issuer.path_for(tmp_path).read_text())["cert"] == entry.cert
    with pytest.raises(ProjectError, match="already has an issuer"):
        issuer.create(tmp_path, PASS)


def test_create_with_the_wrong_passphrase_fails_before_writing(tmp_path):
    roots_for(tmp_path)
    with pytest.raises(ProjectError, match="could not load private key"):
        issuer.create(tmp_path, "nope")
    assert issuer.read(tmp_path) is None


def test_replace_keeps_old_desk_certificates_valid(tmp_path):
    roots = roots_for(tmp_path)
    first = issuer.create(tmp_path, PASS)
    desk = dk.create(tmp_path, "bench-1", PASS)
    second = issuer.replace(tmp_path, PASS)
    assert second.key_id != first.key_id and issuer.read(tmp_path) == second
    # the desk still verifies through the FIRST issuer, which it carries
    d, i = cert_mod.verify_chain(bytes.fromhex(desk.cert), bytes.fromhex(desk.issuer_cert), roots)
    assert i.pubkey.hex() == first.pubkey and d.pubkey.hex() == desk.pubkey
    # and a desk made now chains through the second
    later = dk.create(tmp_path, "bench-2", PASS)
    _, i2 = cert_mod.verify_chain(bytes.fromhex(later.cert), bytes.fromhex(later.issuer_cert),
                                  roots)
    assert i2.pubkey.hex() == second.pubkey


def test_certify_desk_signs_with_the_issuer_and_names_its_root(tmp_path):
    roots = roots_for(tmp_path)
    entry = issuer.create(tmp_path, PASS)
    desk_pub = dk.create(tmp_path / "other", "bench-1", PASS).pubkey    # any P-256 point
    desk_hex, issuer_hex = issuer.certify_desk(tmp_path, desk_pub,
                                               passphrase_provider=lambda: (PASS, "file"))
    assert issuer_hex == entry.cert
    desk, iss = cert_mod.verify_chain(bytes.fromhex(desk_hex), bytes.fromhex(issuer_hex), roots)
    assert desk.pubkey.hex() == desk_pub and desk.kind == cert_mod.KIND_DESK
    assert desk.root_id == iss.root_id == 0 and desk.epoch == 0
    with pytest.raises(ProjectError, match="could not load private key"):
        issuer.certify_desk(tmp_path, desk_pub, passphrase_provider=lambda: ("nope", "file"))


def test_adopt_external_records_a_backend_and_certifies_it(tmp_path):
    roots = roots_for(tmp_path)
    pub = dk.create(tmp_path / "other", "x", PASS).pubkey
    backend = {"backend": "not-a-backend-this-box-has"}
    entry = issuer.adopt_external(tmp_path, PASS, pub, backend)
    assert entry.backend == backend and entry.pubkey == pub
    assert issuer.read(tmp_path).backend == backend
    cert_mod.verify(bytes.fromhex(entry.cert), roots, kinds=(cert_mod.KIND_ISSUER,))
    # its signer dispatches on the backend record, and a bad one is a project error
    with pytest.raises(ProjectError, match="unknown signer backend"):
        issuer.signer(tmp_path, entry)


def test_a_damaged_issuer_file_is_an_error_not_a_crash(tmp_path):
    issuer.path_for(tmp_path).parent.mkdir(parents=True)
    issuer.path_for(tmp_path).write_text("{\"key_id\": 1}")
    with pytest.raises(ProjectError, match="damaged"):
        issuer.read(tmp_path)


def test_replace_and_adopt_drop_the_retired_issuer_s_pem(tmp_path):
    roots_for(tmp_path)
    first = issuer.create(tmp_path, PASS)
    private = tmp_path / "keys" / "private"
    second = issuer.replace(tmp_path, PASS)
    assert sorted(p.name for p in private.glob("issuer-*.pem")) == ["issuer-%04x.pem" % second.key_id]
    assert first.key_id != second.key_id
    pub = dk.create(tmp_path / "o", "x", PASS).pubkey
    issuer.adopt_external(tmp_path, PASS, pub, {"backend": "pkcs11", "uri": "x"})
    assert list(private.glob("issuer-*.pem")) == []        # it lives in the backend now

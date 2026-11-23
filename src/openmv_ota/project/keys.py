"""OTA signing-key lifecycle for a project: status, rotation, revocation, retirement.

A camera embeds only the project's roots; every signing key reaches it as a certificate
a root signed, carried in each image. The key set is provisioned at ``project new --ota``,
and "rotation" advances which key in that pool the build signs with. Normal state is
implicit in ``[ota].signing_key_id``: OTA keys with a lower id are used, it is the current
signer, higher ids are available. Old releases keep verifying. :func:`add_keys` grows the
pool: the roots certify a new key at any time, and cameras accept it as they are.

**Revocation** is for a *compromised* private key: it marks a key ``revoked`` in
``keys/trusted_keys.json`` (kept, never deleted) so the host refuses to sign with it and
``rotate`` skips it. That alone changes nothing on a camera, and is reversible with
:func:`unrevoke_key`.

**Retirement** (:func:`retire_revoked`) is what reaches the fleet. Every certificate carries
an epoch, and a camera refuses a certificate below the highest epoch it has confirmed a
release at. Retiring re-issues the certificate of every key that is not revoked at the next
epoch and leaves the revoked keys' behind: once a camera confirms a release signed after
that, it refuses the revoked keys, and every image signed before, for good.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from openmv_ota.ota import algorithm_for, read_trusted_keys, write_trusted_keys
from openmv_ota.ota.errors import OtaError

from . import config as config_mod
from .errors import ProjectError
from .project import ProjectPaths


@dataclass
class KeyStatus:
    alg_name: str
    signing_key_id: int
    signer_revoked: bool     # the current signer has been revoked -> rotate before building
    ota_ids: list[int]       # the OTA rotation pool, sorted
    factory_ids: list[int]   # factory keys, sorted
    used: int                # OTA keys before the current signer
    remaining: int           # non-revoked OTA keys after the current signer
    revoked: int             # revoked keys in the whole set
    private_present: int     # private PEMs on this machine
    private_total: int       # total keys in the set
    epoch: int = 0           # the certificate epoch releases are signed at
    unretired: int = 0       # revoked keys cameras still accept (`keys retire` not run since)


def _load(root: Path) -> tuple[config_mod.OtaConfig, list, ProjectPaths]:
    paths = ProjectPaths(Path(root))
    config = config_mod.load_config(paths.config)
    if not config.ota or config.signing_key_id is None:
        raise ProjectError("not an OTA project (no signing key); create one with "
                           "`openmv-ota project new --ota`")
    try:
        trusted = read_trusted_keys(paths.trusted_keys)
    except OtaError as e:
        raise ProjectError(str(e)) from None
    return config, trusted, paths


def _find(trusted: list, key_id: int):
    key = next((k for k in trusted if k.key_id == key_id), None)
    if key is None:
        raise ProjectError("no key with id 0x%04x in keys/trusted_keys.json" % key_id)
    return key


def key_epoch(key) -> int:
    """The epoch of ``key``'s certificate."""
    from openmv_ota.ota import certificate
    return certificate.parse(bytes.fromhex(key.cert)).epoch


def current_epoch(trusted: list) -> int:
    """The epoch the project signs at: the highest any key's certificate carries."""
    return max(key_epoch(k) for k in trusted)


def _root_key(paths: ProjectPaths, root: Path, key_passphrase_file):
    """Root 0's private key: what signs certificates."""
    from openmv_ota.ota.keys import load_private_key_pem, root_pem_name

    from . import passphrase as passphrase_mod
    phrase, _ = passphrase_mod.resolve_passphrase(root, passphrase_file=key_passphrase_file)
    try:
        return phrase, load_private_key_pem(
            (paths.private_keys_dir / root_pem_name(0)).read_bytes(), phrase)
    except OSError:
        raise ProjectError("no %s in keys/private: the root key signs the keys' "
                           "certificates, and nothing can take its place" % root_pem_name(0)) from None


def _ota_index(trusted: list, signing_key_id: int) -> tuple[list[int], int]:
    ota_ids = sorted(k.key_id for k in trusted if k.role == "ota")
    if signing_key_id not in ota_ids:
        raise ProjectError(
            "signing key 0x%04x is not an OTA key in keys/trusted_keys.json" % signing_key_id)
    return ota_ids, ota_ids.index(signing_key_id)


def backend_summary(root: Path) -> list[tuple[int, str, str]]:
    """``(key_id, role, backend-tag)`` per trusted key -- ``encrypted-pem`` when there's no record."""
    from .backends import read_backends
    _config, trusted, _paths = _load(root)
    records = read_backends(root)
    return [(k.key_id, k.role, records.get(k.key_id, {}).get("backend", "encrypted-pem"))
            for k in sorted(trusted, key=lambda k: k.key_id)]


def set_backend(root: Path, key_id: int, record: dict) -> None:
    """Point a trusted key at an external backend by writing its ``keys/backends.json`` record.
    (The build's consistency check verifies the backend's public key matches ``trusted_keys.json``.)"""
    from .backends import read_backends, write_backends
    _config, trusted, _paths = _load(root)
    _find(trusted, key_id)   # 404 if unknown
    records = read_backends(root)
    if "pin" in record:
        raise ProjectError("a PIN does not go in keys/backends.json (it is committed): set "
                           "OPENMV_OTA_PKCS11_PIN in the environment that signs")
    for field, env in (("pkcs11_module", "OPENMV_OTA_PKCS11_MODULE"),
                       ("factory", "OPENMV_OTA_SIGNER_FACTORY")):
        if field in record:
            # code the signing machine loads: its own to name, not the project's
            raise ProjectError("%s does not go in keys/backends.json (it comes with the "
                               "project, and names code to load): set %s in the environment "
                               "that signs" % (field, env))
    records[key_id] = record
    write_backends(root, records)


def provision_backend(root: Path, provisioner, *, n_factory: int, n_ota: int,
                      key_passphrase_file=None) -> int:
    """Generate a fresh key set **inside** an external backend and replace ``trusted_keys.json`` +
    ``backends.json`` with it (no signing key's private PEM ever touches disk), and mint the
    project's issuer there too (``keys/issuer.json``). Keeps the current signing algorithm and the
    project's roots: the new keys are certified by root 0, so devices that embed the roots trust
    them with no firmware change. Returns the new signing key id.

    The new set is certified one epoch above the old, which retires every key it replaces:
    their certificates stay valid at the old epoch and their PEMs stay on this disk and in
    every backup, and once they are out of ``trusted_keys.json`` nothing else could ever
    retire them. A camera refuses them from the first release it confirms under the new set."""
    from openmv_ota.ota.signer import provision_external_key_set

    from .backends import write_backends
    _config, existing, paths = _load(root)
    alg = algorithm_for(existing[0].alg)
    phrase, root_priv = _root_key(paths, root, key_passphrase_file)
    trusted, records, signing_key_id = provision_external_key_set(
        alg, n_factory, n_ota, provisioner, root_priv, epoch=current_epoch(existing) + 1)
    write_trusted_keys(paths.trusted_keys, trusted)
    write_backends(root, records)
    config_mod.set_signing_key_id(paths.config, signing_key_id)
    # The issuer lives like a signing key, so it moves with them: a fresh one minted in the
    # backend and certified by the same root. Desks already certified keep their chain.
    from . import issuer as issuer_mod
    pubkey, record = provisioner.provision(issuer_mod.ISSUER_LABEL_ID, issuer_mod.ROLE, alg)
    issuer_mod.adopt_external(root, phrase, pubkey, record)
    return signing_key_id


def key_status(root: Path) -> KeyStatus:
    """Resolve the project's OTA key status (read-only)."""
    config, trusted, paths = _load(root)
    ota_ids, idx = _ota_index(trusted, config.signing_key_id)
    by_id = {k.key_id: k for k in trusted}
    signer = by_id[config.signing_key_id]
    present = (len([p for p in paths.private_keys_dir.glob("*.pem")
                    if not p.name.startswith(("root-", "issuer-"))])  # neither signs images
               if paths.private_keys_dir.exists() else 0)
    return KeyStatus(
        alg_name=algorithm_for(signer.alg).name,
        signing_key_id=config.signing_key_id,
        signer_revoked=signer.revoked,
        ota_ids=ota_ids,
        factory_ids=sorted(k.key_id for k in trusted if k.role == "factory"),
        used=idx,
        remaining=sum(1 for kid in ota_ids[idx + 1:] if not by_id[kid].revoked),
        revoked=sum(1 for k in trusted if k.revoked),
        private_present=present,
        private_total=len(trusted),
        epoch=current_epoch(trusted),
        unretired=sum(1 for k in trusted if k.revoked and key_epoch(k) == current_epoch(trusted)),
    )


def rotate_signing_key(root: Path) -> tuple[int, int, list[str]]:
    """Advance the OTA signing key to the next **non-revoked** key in the pool. Updates
    the config and returns ``(old_id, new_id, warnings)``. Raises when none remain."""
    config, trusted, paths = _load(root)
    ota_ids, idx = _ota_index(trusted, config.signing_key_id)
    by_id = {k.key_id: k for k in trusted}
    new = next((kid for kid in ota_ids[idx + 1:] if not by_id[kid].revoked), None)
    if new is None:
        raise ProjectError(
            "no more OTA keys in the pool (all later keys used or revoked)", exit_code=1)
    old = config.signing_key_id
    warnings: list[str] = []
    pem = paths.private_keys_dir / ("ota-%04x.pem" % new)
    if not pem.exists():
        warnings.append("private key %s is not on this machine; builds will fail to sign "
                        "until it is provisioned here" % pem)
    config_mod.set_signing_key_id(paths.config, new)
    return old, new, warnings


def revoke_key(root: Path, key_id: int) -> tuple[object, bool, bool]:
    """Mark ``key_id`` revoked in the trusted set (kept, not deleted). Returns
    ``(key, changed, is_signer)``: ``changed`` is False if it was already revoked;
    ``is_signer`` is True if it is the current signing key (so the caller can tell the
    user to rotate). Does not advance the signer (build refuses a revoked signer)."""
    config, trusted, paths = _load(root)
    key = _find(trusted, key_id)
    is_signer = key_id == config.signing_key_id
    if key.revoked:
        return key, False, is_signer
    key.revoked = True
    write_trusted_keys(paths.trusted_keys, trusted)
    return key, True, is_signer


def unrevoke_key(root: Path, key_id: int) -> tuple[object, bool]:
    """Clear ``revoked`` on ``key_id``. Returns ``(key, changed)`` (``changed`` False
    if it wasn't revoked). Does not rewind the signer if a rotation moved past it."""
    _config, trusted, paths = _load(root)
    key = _find(trusted, key_id)
    if not key.revoked:
        return key, False
    if key_epoch(key) < current_epoch(trusted):
        raise ProjectError(
            "key 0x%04x was retired: its certificate was left at epoch %d and the project "
            "signs at %d, so cameras refuse it and nothing brings it back"
            % (key_id, key_epoch(key), current_epoch(trusted)))
    key.revoked = False
    write_trusted_keys(paths.trusted_keys, trusted)
    return key, True


def retire_revoked(root: Path, *, key_passphrase_file=None) -> tuple[int, list[int]]:
    """Make the fleet refuse every revoked key: re-issue the certificate of each key that is
    not revoked at the next epoch (signed by root 0), leaving the revoked keys' at the old one.
    Returns ``(new_epoch, retired_key_ids)``. A camera refuses the retired keys from the
    moment it confirms a release signed after this; raises when no revoked key is waiting."""
    from openmv_ota.ota.keys import certify
    _config, trusted, paths = _load(root)
    epoch = current_epoch(trusted)
    retired = sorted(k.key_id for k in trusted if k.revoked and key_epoch(k) == epoch)
    if not retired:
        raise ProjectError("no revoked key to retire: `project keys revoke` one first")
    live = [k for k in trusted if not k.revoked]
    if not any(k.role == "ota" for k in live):
        raise ProjectError("every OTA key is revoked: nothing would be left to sign a release")
    _phrase, root_priv = _root_key(paths, root, key_passphrase_file)
    certify(root_priv, 0, live, epoch=epoch + 1)
    write_trusted_keys(paths.trusted_keys, trusted)
    return epoch + 1, retired


def add_keys(root: Path, *, n_ota: int = 0, n_factory: int = 0, provisioner=None,
             key_passphrase_file=None) -> list[int]:
    """Grow the key set: mint ``n_ota`` OTA and ``n_factory`` factory keys, certified by root 0
    at the project's current epoch, so cameras accept them as they are. The new ids follow
    the highest of their role (an id is never reused). With ``provisioner`` the keys are made
    inside that external backend; otherwise each is an encrypted PEM in ``keys/private``.
    Returns the new key ids. The signer does not move: ``rotate`` reaches the new OTA keys."""
    from openmv_ota.ota.keys import (
        FACTORY_KEY_ID_BASE,
        OTA_KEY_ID_BASE,
        TrustedKey,
        certify,
        generate_private_key,
        private_key_pem,
        public_point_hex,
        write_private,
    )

    from .backends import read_backends, write_backends
    if n_ota < 0 or n_factory < 0 or n_ota + n_factory == 0:
        raise ProjectError("nothing to add: give --ota-keys N and/or --factory-keys N")
    _config, trusted, paths = _load(root)
    alg = algorithm_for(trusted[0].alg)
    # (role, how many, first id of its range, one past its last id)
    ranges = (("factory", n_factory, FACTORY_KEY_ID_BASE, OTA_KEY_ID_BASE),
              ("ota", n_ota, OTA_KEY_ID_BASE, 0x10000))
    plan: list[tuple[int, str]] = []
    for role, count, base, end in ranges:
        first = max([k.key_id + 1 for k in trusted if k.role == role], default=base)
        if first + count > end:
            raise ProjectError("no room for %d more %s keys: ids 0x%04x-0x%04x are the %s "
                               "range and 0x%04x is the next free" % (count, role, base, end - 1,
                                                                    role, first))
        plan += [(first + i, role) for i in range(count)]
    phrase, root_priv = _root_key(paths, root, key_passphrase_file)
    new: list = []
    pems: dict[str, bytes] = {}
    records = read_backends(root)
    for key_id, role in plan:
        if provisioner is not None:
            pubkey, records[key_id] = provisioner.provision(key_id, role, alg)
        else:
            priv = generate_private_key(alg)
            pubkey = public_point_hex(priv.public_key())
            pems["%s-%04x.pem" % (role, key_id)] = private_key_pem(priv, phrase)
        new.append(TrustedKey(key_id, alg.cose_id, role, pubkey))
    certify(root_priv, 0, new, epoch=current_epoch(trusted))
    for name, pem in pems.items():
        write_private(paths.private_keys_dir / name, pem)
    if provisioner is not None:
        write_backends(root, records)
    write_trusted_keys(paths.trusted_keys, sorted(trusted + new, key=lambda k: k.key_id))
    return [k.key_id for k in new]

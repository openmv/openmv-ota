"""OTA signing-key lifecycle for a project: status, rotation, revocation.

The full key set is provisioned once at ``project new --ota`` — you can't add a
trusted key later without re-flashing firmware with a new baked-in set. So
"rotation" doesn't mint a key; it advances which key in the **pre-provisioned**
OTA pool the build signs with. Normal state is implicit in ``[ota].signing_key_id``:
OTA keys with a lower id are retired, it is the current signer, higher ids are
available. Old releases keep verifying — their key stays trusted.

**Revocation** is the rare exception, for a *compromised* private key: it marks a
key ``revoked`` in ``keys/trusted_keys.json`` (kept, never deleted) so the host
refuses to sign with it and ``rotate`` skips it. The device-side reject-list is
honored by a future firmware build, so revocation only fully protects devices once
they update — already-fielded devices keep trusting the key until then. It is
reversible with :func:`unrevoke_key`.
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
    retired: int             # OTA keys before the current signer
    remaining: int           # non-revoked OTA keys after the current signer
    revoked: int             # revoked keys in the whole set
    private_present: int     # private PEMs on this machine
    private_total: int       # total keys in the set


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


def _ota_index(trusted: list, signing_key_id: int) -> tuple[list[int], int]:
    ota_ids = sorted(k.key_id for k in trusted if k.role == "ota")
    if signing_key_id not in ota_ids:
        raise ProjectError(
            "signing key 0x%04x is not an OTA key in keys/trusted_keys.json" % signing_key_id)
    return ota_ids, ota_ids.index(signing_key_id)


def encrypt_private_keys(root: Path, passphrase: str) -> list[str]:
    """Migrate a project created before encryption-at-rest: re-write each **plaintext** private PEM
    as an encrypted PEM under ``passphrase``. Keys that are already encrypted are left untouched (we
    can't decrypt them without their passphrase). Returns the filenames re-encrypted."""
    from openmv_ota.ota.keys import load_private_key_pem, private_key_pem
    _config, _trusted, paths = _load(root)   # validates it's an OTA project
    done: list[str] = []
    for pem_path in sorted(paths.private_keys_dir.glob("*.pem")):
        try:
            key = load_private_key_pem(pem_path.read_bytes(), None)   # a plaintext PEM?
        except OtaError:
            continue                                                 # already encrypted -> skip
        pem_path.write_bytes(private_key_pem(key, passphrase))
        done.append(pem_path.name)
    return done


def key_status(root: Path) -> KeyStatus:
    """Resolve the project's OTA key status (read-only)."""
    config, trusted, paths = _load(root)
    ota_ids, idx = _ota_index(trusted, config.signing_key_id)
    by_id = {k.key_id: k for k in trusted}
    signer = by_id[config.signing_key_id]
    present = (len(list(paths.private_keys_dir.glob("*.pem")))
               if paths.private_keys_dir.exists() else 0)
    return KeyStatus(
        alg_name=algorithm_for(signer.alg).name,
        signing_key_id=config.signing_key_id,
        signer_revoked=signer.revoked,
        ota_ids=ota_ids,
        factory_ids=sorted(k.key_id for k in trusted if k.role == "factory"),
        retired=idx,
        remaining=sum(1 for kid in ota_ids[idx + 1:] if not by_id[kid].revoked),
        revoked=sum(1 for k in trusted if k.revoked),
        private_present=present,
        private_total=len(trusted),
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
            "no more OTA keys in the pool (all later keys used or revoked); adding keys "
            "needs a firmware re-flash with a new trusted set", exit_code=1)
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
    key.revoked = False
    write_trusted_keys(paths.trusted_keys, trusted)
    return key, True

"""Pluggable signing backends.

A ``Signer`` abstracts the one thing a build needs — produce a raw ``R||S`` signature over a
region with a trusted key — so the private key can live in an **encrypted PEM** (the default),
a PKCS#11 token, a cloud KMS, or a custom backend, and never as plaintext on disk. ``build_signer``
is the settings-driven factory (mirrors ``server.build_storage``/``build_metastore``); heavy
backends are imported lazily behind pip extras. The OTA *server* never signs — this is entirely
build/client-side.
"""

from __future__ import annotations

import abc
import importlib
from pathlib import Path

from .algorithms import AlgSpec
from .errors import OtaError
from .keys import load_private_key_pem, public_point_hex
from .sign import sign_region


class Signer(abc.ABC):
    """Produces raw ``R||S`` signatures for one trusted key. ``key_id``/``sig_alg``/``alg`` describe
    the key (written into the trailer/manifest headers); ``is_dev_key`` marks a local dev key that
    the production build rail refuses."""

    key_id: int
    sig_alg: int          # COSE id
    alg: AlgSpec
    is_dev_key: bool = False

    @abc.abstractmethod
    def sign(self, region: bytes) -> bytes:
        """The fixed-width raw ``R||S`` signature (``alg.sig_size`` bytes) over ``region``."""

    @abc.abstractmethod
    def public_point_hex(self) -> str:
        """The signer's public key as an uncompressed EC point (``04||X||Y``) hex, for the
        build-time check against ``keys/trusted_keys.json``."""


class LocalSigner(Signer):
    """Signs with a ``cryptography`` private key loaded from an (encrypted) PEM."""

    def __init__(self, private_key, key_id: int, sig_alg: int, alg: AlgSpec, *, is_dev_key=False):
        self._key = private_key
        self.key_id = key_id
        self.sig_alg = sig_alg
        self.alg = alg
        self.is_dev_key = is_dev_key

    def sign(self, region: bytes) -> bytes:
        return sign_region(self._key, region, self.alg)

    def public_point_hex(self) -> str:
        return public_point_hex(self._key.public_key())


def build_signer(entry, alg: AlgSpec, *, private_keys_dir: Path, backend: dict | None = None,
                 passphrase_provider=None) -> Signer:
    """Resolve the ``Signer`` for a trusted-key ``entry`` (a ``TrustedKey``). Dispatches on the
    ``keys/backends.json`` record's ``backend`` tag (default: a local encrypted PEM). Raises
    ``OtaError`` for an unknown tag or an unreachable/mis-typed key."""
    tag = (backend or {}).get("backend") or "encrypted-pem"
    if tag == "encrypted-pem":
        return _local_signer(entry, alg, private_keys_dir, passphrase_provider)
    if tag == "pkcs11":
        from . import signer_pkcs11
        return signer_pkcs11.build(entry, alg, backend)
    if tag in ("aws-kms", "gcp-kms", "azure-kms"):
        from . import signer_kms
        return signer_kms.build(entry, alg, backend)
    if tag == "custom":
        return _custom_signer(entry, alg, backend or {})
    raise OtaError("unknown signer backend: %r" % tag)


def _local_signer(entry, alg, private_keys_dir, passphrase_provider):
    pem_path = private_keys_dir / ("%s-%04x.pem" % (entry.role, entry.key_id))
    try:
        data = pem_path.read_bytes()
    except OSError:
        raise OtaError(
            "private key %s not found - only the signing machine has it; build the body without "
            "signing elsewhere, or provision the key here" % pem_path) from None
    passphrase, source = passphrase_provider() if passphrase_provider is not None else (None, None)
    return LocalSigner(load_private_key_pem(data, passphrase), entry.key_id, entry.alg, alg,
                       is_dev_key=(source == "dev"))


class KeyProvisioner(abc.ABC):
    """Generates a keypair **inside** an external backend (token/KMS) and returns its public point +
    the ``keys/backends.json`` record for reaching it -- so the private key never lands on disk."""

    @abc.abstractmethod
    def provision(self, key_id: int, role: str, alg: AlgSpec) -> tuple[str, dict]:
        """Return ``(uncompressed_public_point_hex, backend_record)`` for a freshly generated key."""


def provision_external_key_set(alg: AlgSpec, n_factory: int, n_ota: int,
                               provisioner: KeyProvisioner):
    """Mint the whole pool in an external backend. Returns ``(trusted_keys, backend_records,
    signing_key_id)`` -- the public set for ``keys/trusted_keys.json`` and the per-key records for
    ``keys/backends.json``. No private PEM is ever produced."""
    from .keys import FACTORY_KEY_ID_BASE, OTA_KEY_ID_BASE, TrustedKey
    trusted: list = []
    records: dict[int, dict] = {}

    def _mint(key_id: int, role: str) -> None:
        pubkey, record = provisioner.provision(key_id, role, alg)
        trusted.append(TrustedKey(key_id, alg.cose_id, role, pubkey))
        records[key_id] = record

    for i in range(n_factory):
        _mint(FACTORY_KEY_ID_BASE + i, "factory")
    for i in range(n_ota):
        _mint(OTA_KEY_ID_BASE + i, "ota")
    return trusted, records, OTA_KEY_ID_BASE


def build_provisioner(backend: dict) -> KeyProvisioner:
    """Resolve the ``KeyProvisioner`` for a ``keys/backends.json``-shaped ``backend`` record.
    Only external backends can provision (a local PEM would defeat the purpose). Mirrors
    ``build_signer`` dispatch; raises ``OtaError`` for a local/unknown tag."""
    tag = (backend or {}).get("backend")
    if tag == "pkcs11":
        from . import signer_pkcs11
        return signer_pkcs11.provisioner(backend)
    if tag in ("aws-kms", "gcp-kms", "azure-kms"):
        from . import signer_kms
        return signer_kms.provisioner(backend)
    if tag in (None, "encrypted-pem"):
        raise OtaError("the %s backend can't provision keys externally; it writes a local "
                       "encrypted PEM. Use `project new` for local keys." % (tag or "encrypted-pem"))
    raise OtaError("unknown provisioning backend: %r" % tag)


def _custom_signer(entry, alg, backend):
    ref = backend.get("factory")
    if not ref or ":" not in ref:
        raise OtaError("custom signer backend needs a 'factory' = 'pkg.module:callable'")
    mod_name, _, attr = ref.partition(":")
    try:
        factory = getattr(importlib.import_module(mod_name), attr)
    except (ImportError, AttributeError) as e:
        raise OtaError("custom signer factory %r not importable: %s" % (ref, e)) from None
    signer = factory(entry, alg, backend)
    if not isinstance(signer, Signer):
        raise OtaError("custom signer factory %r did not return a Signer" % ref)
    return signer

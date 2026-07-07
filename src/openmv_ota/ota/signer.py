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

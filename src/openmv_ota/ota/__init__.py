"""OTA trailer codec — the signed on-flash trailer format for OpenMV OTA images.

``trailer.py`` is the byte-layout source-of-truth, ``algorithms.py`` the COSE
signature-algorithm registry, and ``geometry.py`` the slot/erase-block geometry —
all pure and dependency-free. The crypto-backed helpers (key generation, signing,
verification) live in ``keys.py`` / ``sign.py`` and are imported **lazily**, so
importing this package for the pure codec or slot geometry never pulls in
``cryptography``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .algorithms import ES256, ES384, ES512, AlgSpec, algorithm_for
from .errors import OtaError
from .trailer import (
    HEADER_SIZE,
    HEADER_VERSION,
    MAGIC_FIRMWARE,
    MAGIC_ROMFS_APP,
    TRAILER_SZ,
    Trailer,
    pack_trailer,
    parse_trailer,
    signed_region,
)

if TYPE_CHECKING:  # for type checkers + __all__; not imported at runtime
    from .keys import (
        ProvisionedKeys,
        TrustedKey,
        generate_private_key,
        load_private_key_pem,
        private_key_pem,
        provision_key_set,
        public_key_from_hex,
        public_point_hex,
        read_trusted_keys,
        write_trusted_keys,
    )
    from .sign import sign_region, verify_region

# Public name -> submodule, imported on first access so `cryptography` stays off
# the pure-codec / geometry import path.
_LAZY = {
    "sign_region": "sign",
    "verify_region": "sign",
    "generate_private_key": "keys",
    "public_point_hex": "keys",
    "public_key_from_hex": "keys",
    "private_key_pem": "keys",
    "load_private_key_pem": "keys",
    "TrustedKey": "keys",
    "read_trusted_keys": "keys",
    "write_trusted_keys": "keys",
    "ProvisionedKeys": "keys",
    "provision_key_set": "keys",
}


def __getattr__(name: str):
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError("module %r has no attribute %r" % (__name__, name))
    import importlib

    return getattr(importlib.import_module("." + module, __name__), name)


__all__ = [
    "OtaError",
    "AlgSpec",
    "algorithm_for",
    "ES256",
    "ES384",
    "ES512",
    "Trailer",
    "pack_trailer",
    "parse_trailer",
    "signed_region",
    "MAGIC_ROMFS_APP",
    "MAGIC_FIRMWARE",
    "HEADER_SIZE",
    "HEADER_VERSION",
    "TRAILER_SZ",
    "sign_region",
    "verify_region",
    "generate_private_key",
    "public_point_hex",
    "public_key_from_hex",
    "private_key_pem",
    "load_private_key_pem",
    "TrustedKey",
    "read_trusted_keys",
    "write_trusted_keys",
    "ProvisionedKeys",
    "provision_key_set",
]

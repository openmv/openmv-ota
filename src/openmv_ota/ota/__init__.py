"""OTA trailer codec — the signed on-flash trailer format for OpenMV OTA images.

Pure and dependency-free, with no crypto: the signature is opaque bytes produced
and verified by a separate layer. ``trailer.py`` is the byte-layout source-of-truth
and ``algorithms.py`` is the COSE signature-algorithm registry.
"""

from __future__ import annotations

from .algorithms import EDDSA, ES256, ES256K, ES384, ES512, AlgSpec, algorithm_for
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

__all__ = [
    "OtaError",
    "AlgSpec",
    "algorithm_for",
    "ES256",
    "ES384",
    "ES512",
    "ES256K",
    "EDDSA",
    "Trailer",
    "pack_trailer",
    "parse_trailer",
    "signed_region",
    "MAGIC_ROMFS_APP",
    "MAGIC_FIRMWARE",
    "HEADER_SIZE",
    "HEADER_VERSION",
    "TRAILER_SZ",
]

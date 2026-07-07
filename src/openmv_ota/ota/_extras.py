"""Guards for the optional signer-backend dependency extras.

A signer backend module (``signer_pkcs11``/``signer_kms``) is imported lazily by ``build_signer``,
and the heavy SDK it needs (python-pkcs11 / boto3 / google-cloud-kms / azure-keyvault) is imported
*after* the matching guard here turns a missing extra into a clear ``pip install`` hint rather than
a raw ``ImportError``. Modelled on ``server/_extras.py``.
"""

from __future__ import annotations

import importlib
from typing import Callable

from .errors import OtaError

# extra name -> (probe import, the pip extra to install)
_EXTRAS = {
    "hsm": ("pkcs11", "hsm"),
    "aws-kms": ("boto3", "aws-kms"),
    "gcp-kms": ("google.cloud.kms", "gcp-kms"),
    "azure-kms": ("azure.keyvault.keys", "azure-kms"),
}


def require_extra(name: str, _import: Callable[[str], object] | None = None) -> None:
    """Raise ``OtaError`` (exit 2) unless the backend extra ``name`` is importable. ``_import`` is a
    test seam (defaults to ``importlib.import_module``)."""
    probe, extra = _EXTRAS[name]
    imp = _import or importlib.import_module
    try:
        imp(probe)
    except ImportError:
        raise OtaError("the %r signer backend needs extra packages -- run: pip install "
                       "openmv-ota[%s]" % (name, extra), exit_code=2) from None

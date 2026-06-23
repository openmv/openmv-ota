"""The ota package loads crypto-backed helpers lazily (keeps `cryptography` off the
pure-codec / geometry import path)."""

from __future__ import annotations

import pytest

import openmv_ota.ota as ota


def test_lazy_crypto_helpers_resolve():
    # Accessing a keys/sign symbol imports its submodule on demand.
    assert ota.read_trusted_keys.__name__ == "read_trusted_keys"
    assert ota.sign_region.__name__ == "sign_region"


def test_unknown_attribute_raises():
    with pytest.raises(AttributeError, match="does_not_exist"):
        getattr(ota, "does_not_exist")

"""Tests for the COSE algorithm registry."""

from __future__ import annotations

import pytest

from openmv_ota.ota import ES256, ES384, ES512, algorithm_for
from openmv_ota.ota.errors import OtaError


def test_supported_specs():
    es256 = algorithm_for(ES256)
    assert (es256.name, es256.curve, es256.hash_name) == ("ES256", "secp256r1", "sha256")
    assert (es256.sig_size, es256.pubkey_size) == (64, 65)

    assert algorithm_for(ES384).sig_size == 96
    assert algorithm_for(ES384).pubkey_size == 97
    assert algorithm_for(ES512).sig_size == 132
    assert algorithm_for(ES512).pubkey_size == 133


def test_unknown_id_raises():
    # Any id outside the P-curve set is rejected (e.g. ES256K -47, EdDSA -8).
    with pytest.raises(OtaError, match="unknown COSE algorithm id 999"):
        algorithm_for(999)
    with pytest.raises(OtaError, match="unknown COSE algorithm id -47"):
        algorithm_for(-47)

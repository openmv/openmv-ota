"""The signer-backend extra guards (require_extra)."""

from __future__ import annotations

import pytest

from openmv_ota.ota._extras import require_extra
from openmv_ota.ota.errors import OtaError


def test_present_is_ok():
    require_extra("hsm", _import=lambda name: object())     # importable -> no raise


def test_missing_gives_pip_hint():
    def _missing(name):
        raise ImportError(name)

    with pytest.raises(OtaError, match=r"pip install openmv-ota\[hsm\]") as e:
        require_extra("hsm", _import=_missing)
    assert e.value.exit_code == 2
    with pytest.raises(OtaError, match=r"openmv-ota\[aws-kms\]"):
        require_extra("aws-kms", _import=_missing)

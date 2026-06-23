"""Tests for app version (semver) <-> payload_version encoding."""

from __future__ import annotations

import pytest

from openmv_ota.ota.errors import OtaError
from openmv_ota.ota.version import encode_app_version, parse_semver


def test_parse_semver():
    assert parse_semver("1.2.3") == (1, 2, 3)
    assert parse_semver("  10.0.255 ") == (10, 0, 255)


@pytest.mark.parametrize("bad", ["1.2", "1.2.3.4", "v1.2.3", "1.2.x", "", "256.0.0"])
def test_parse_semver_rejects(bad):
    with pytest.raises(OtaError):
        parse_semver(bad)


def test_encode_app_version():
    assert encode_app_version("1.0.0") == 0x01000000
    assert encode_app_version("1.2.3") == 0x01020300
    assert encode_app_version("0.0.1") == 0x00000100
    # Monotonic: a later semver encodes to a larger uint32.
    assert encode_app_version("1.2.4") > encode_app_version("1.2.3")
    assert encode_app_version("2.0.0") > encode_app_version("1.99.99")

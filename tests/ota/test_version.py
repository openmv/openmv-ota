"""Tests for app version (semver) <-> payload_version encoding."""

from __future__ import annotations

import pytest

from openmv_ota.ota.errors import OtaError
from openmv_ota.ota.version import decode_app_version, encode_app_version, parse_semver


def test_parse_semver():
    assert parse_semver("1.2.3") == (1, 2, 3, 0)
    assert parse_semver("  10.0.255 ") == (10, 0, 255, 0)


def test_the_build_byte_is_a_fourth_component():
    """Three components reach 2**24 versions, which is a low ceiling for an operator
    publishing a build per customer into one shared ordering. The fourth multiplies it by
    256 and leaves the human-facing number alone."""
    assert parse_semver("1.2.3.7") == (1, 2, 3, 7)
    assert encode_app_version("1.2.3.7") == encode_app_version("1.2.3") + 7
    # a three-component version encodes exactly as it always did
    assert encode_app_version("1.2.3.0") == encode_app_version("1.2.3")
    assert decode_app_version(encode_app_version("1.2.3")) == "1.2.3"
    assert decode_app_version(encode_app_version("1.2.3.7")) == "1.2.3.7"
    # and the ordering a device compares on still follows the text
    assert encode_app_version("1.4.2.255") < encode_app_version("1.4.3")
    assert encode_app_version("1.4.2") < encode_app_version("1.4.2.1")


@pytest.mark.parametrize("bad", ["1.2", "1.2.3.4.5", "v1.2.3", "1.2.x", "", "256.0.0",
                                 "1.2.3.256"])
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


def test_decode_app_version():
    assert decode_app_version(encode_app_version("2.5.0")) == "2.5.0"
    assert decode_app_version(5 << 24) == "5.0.0"
    assert decode_app_version(0) == "0.0.0"
    # the low build byte renders only when non-zero
    assert decode_app_version((1 << 24) | (2 << 16) | (3 << 8) | 4) == "1.2.3.4"

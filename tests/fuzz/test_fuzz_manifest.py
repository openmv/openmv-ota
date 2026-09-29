"""Fuzz the signed update manifest: the host codec + verifier, and the installer's own
parse/verify/vet path -- which runs on the device BEFORE anything is erased, against bytes that
came off the network."""

from __future__ import annotations

import json
import struct

from hypothesis import given
from hypothesis import strategies as st

from openmv_ota.ota import manifest as M
from openmv_ota.ota.errors import OtaError
from openmv_ota.ota.verify import verify_manifest
from tests.fuzz import fuzzlib as F

INST = F.load_installer()
_GOOD = F.signed_manifest(F.manifest_body())


class _Cfg:
    TRUSTED_KEYS = {F.KEY_ID: F.keypair()[1]}
    PRODUCT_ID = 0x1234
    PLATFORM_VERSION = 5 << 24
    ACCOUNT_ID = ""


def _host(data):
    try:
        return M.parse_manifest(data)
    except OtaError:
        return None


def _device(data):
    try:
        return INST._manifest_parse(data)
    except ValueError:
        return None


def _vet(data):
    """The installer's whole pre-erase manifest phase. OSError is its documented rejection;
    ValueError is the structural parse's."""
    try:
        return INST._vet_manifest("https://h/m.bin", data, _Cfg, F.device_verify, 0, 0, True, "")
    except (OSError, ValueError):
        return None


@st.composite
def framed_manifests(draw, body_values=F.json_values):
    """CRC-valid manifests with arbitrary headers and JSON (or non-JSON) bodies."""
    alg = draw(st.sampled_from((-7, -35, -36)) | st.integers(-(1 << 31), (1 << 31) - 1))
    sig = {-7: 64, -35: 96, -36: 132}.get(alg, 64)
    body = draw(st.binary(max_size=64) | body_values.map(lambda v: json.dumps(v).encode()))
    header = struct.pack(
        M.HEADER_STRUCT,
        draw(st.just(M.MAGIC) | st.binary(min_size=4, max_size=4)),
        draw(st.just(M.HEADER_VERSION) | st.integers(0, 3)),
        draw(st.just(len(body)) | st.integers(0, (1 << 32) - 1)),
        draw(st.just(sig) | st.integers(0, 200)),
        draw(st.integers(0, (1 << 32) - 1)), alg)
    out = header + body + draw(st.binary(min_size=sig, max_size=sig))
    return F.with_crc(out) + draw(st.binary(max_size=16))


@given(st.binary(max_size=9000))
def test_arbitrary_bytes_parse_or_raise_the_documented_error(data):
    _host(data)
    _device(data)
    _vet(data)


@given(framed_manifests(body_values=F.json_values.filter(lambda v: isinstance(v, dict))))
def test_crc_valid_manifests_parse_or_raise_and_the_two_parsers_agree(data):
    host, dev = _host(data), _device(data)
    assert (host is None) == (dev is None)
    if host is not None:
        assert host.body == dev["body"]
        assert (host.key_id, host.sig_alg, host.signature) == (
            dev["key_id"], dev["sig_alg"], dev["signature"])
        assert M.signed_region(data) == dev["region"]
    # Unsigned (a zero key never verifies), so vetting must refuse it -- cleanly.
    assert _vet(data) is None


@given(st.dictionaries(st.text(max_size=8), F.json_values, max_size=6),
       st.sampled_from(F.ALGS), st.integers(0, (1 << 32) - 1))
def test_pack_then_parse_round_trips(body, alg, key_id):
    m = M.Manifest(body=body, key_id=key_id, sig_alg=alg,
                   signature=bytes(M.algorithm_for(alg).sig_size))
    back = M.parse_manifest(M.pack_manifest(m))
    assert back == m


@given(F.mutated(_GOOD))
def test_no_edit_to_a_genuine_manifest_is_accepted_unless_inert(bad):
    ok, _reason = verify_manifest(bad, F.trusted())
    if ok:
        assert bad[:len(_GOOD)] == _GOOD
    if _vet(bad) is not None:
        assert bad[:len(_GOOD)] == _GOOD


def test_the_genuine_manifest_is_accepted():
    assert verify_manifest(_GOOD, F.trusted())[0]
    assert _vet(_GOOD) is not None

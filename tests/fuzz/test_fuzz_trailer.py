"""Fuzz the signed image trailer: the host codec (``openmv_ota.ota.trailer``), the host
verifier, and the device's own parser + slot evaluation in the frozen ``boot.py``.

The contract under test: every parser returns a result or raises its ONE documented error
(``OtaError`` on the host, ``OtaReject`` on the device), and no edit to a genuine image --
trailer or body -- is ever accepted unless it left every authenticated byte untouched."""

from __future__ import annotations

import hashlib
import struct

from hypothesis import given
from hypothesis import strategies as st

from openmv_ota.build.device import boot as B
from openmv_ota.ota import trailer as T
from openmv_ota.ota.errors import OtaError
from openmv_ota.ota.verify import verify_image
from tests import trust
from tests.fuzz import fuzzlib as F

_BODY = bytes(range(256)) * 3
_GOOD = F.signed_trailer(_BODY)
_CERT = trust.certificate(F.keypair()[0])            # a genuine certificate for the fuzz key


def _host(data):
    try:
        return T.parse_trailer(data)
    except OtaError:
        return None


def _device(data):
    try:
        return B.parse_trailer(data)
    except B.OtaReject:
        return None


@st.composite
def framed_trailers(draw):
    """A trailer whose CRC is RIGHT but whose fields are not: correct magic/version and a
    well-formed certificate most of the time (so the fuzzer gets past the cheap checks),
    arbitrary counts, sizes and meta bytes."""
    meta = draw(st.binary(max_size=64) | F.json_values.map(
        lambda v: T.json.dumps(v).encode()))
    n_comps = draw(st.integers(0, 3))
    comp_count = draw(st.just(n_comps) | st.integers(0, (1 << 32) - 1))
    meta_size = draw(st.just(len(meta)) | st.integers(0, (1 << 32) - 1))
    header = struct.pack(
        T.HEADER_STRUCT,
        draw(st.just(T.MAGIC_ROMFS_APP) | st.binary(min_size=4, max_size=4)),
        draw(st.just(T.HEADER_VERSION) | st.integers(0, 6)),
        draw(st.integers(0, (1 << 32) - 1)), comp_count, meta_size,
        draw(st.integers(0, (1 << 32) - 1)), draw(st.integers(0, (1 << 32) - 1)),
        draw(st.integers(0, 255)), draw(st.integers(0, 255)),
        draw(st.integers(0, 255)), draw(st.integers(0, 255)),
        draw(st.integers(0, (1 << 32) - 1)), draw(st.integers(0, (1 << 32) - 1)),
        draw(st.integers(0, (1 << 64) - 1)), draw(st.integers(0, (1 << 64) - 1)),
        draw(st.integers(0, (1 << 64) - 1)), draw(st.binary(min_size=32, max_size=32)))
    comps = draw(st.binary(min_size=48 * n_comps, max_size=48 * n_comps))
    cert = draw(st.just(_CERT) | st.binary(min_size=160, max_size=160))
    body = header + comps + cert + meta + draw(st.binary(min_size=64, max_size=64))
    return F.with_crc(body) + draw(st.binary(max_size=16))


@given(st.binary(max_size=4200))
def test_arbitrary_bytes_parse_or_raise_the_documented_error(data):
    _host(data)
    _device(data)


@given(framed_trailers())
def test_crc_valid_trailers_parse_or_raise_and_the_two_parsers_agree(data):
    host, dev = _host(data), _device(data)
    if host is None:
        return
    assert isinstance(host.meta, dict)
    # The device parser is structural only (it never reads the JSON meta), so anything the
    # host accepts the device must accept too -- with the SAME authenticated fields.
    assert dev is not None
    for f in ("body_size", "product_id", "publish_seq", "min_stub", "payload_version",
              "sig_alg", "body_sha256", "signature", "uart_en", "uart_id", "wdt_en", "wdt_id",
              "baud", "wdt_timeout_ms"):
        assert getattr(host, f) == getattr(dev, f), f
    assert [(c.dest, c.offset, c.size, c.installed_size, c.sha256, c.installed_sha256)
            for c in host.components] \
        == [tuple(c) for c in dev.components]
    assert host.cert.pubkey == dev.pubkey and host.cert.epoch == dev.epoch
    assert dev.signed_region == T.signed_region(data)


_u32 = st.integers(0, (1 << 32) - 1)
_components = st.lists(st.builds(T.Component, dest=_u32, offset=_u32, size=_u32,
                                 installed_size=_u32,
                                 sha256=st.binary(min_size=32, max_size=32),
                                 installed_sha256=st.binary(min_size=32, max_size=32)),
                      max_size=3)


@given(st.builds(
    T.Trailer, body_size=_u32,
    meta=st.dictionaries(st.text(max_size=8), F.json_values, max_size=4),
    product_id=st.integers(0, (1 << 64) - 1), payload_version=_u32, min_stub=_u32,
    body_sha256=st.binary(min_size=32, max_size=32), certificate=st.just(_CERT),
    components=_components,
    uart_en=st.integers(0, 255), uart_id=st.integers(0, 255), wdt_en=st.integers(0, 255),
    wdt_id=st.integers(0, 255), baud=_u32, wdt_timeout_ms=_u32,
    publish_seq=st.integers(0, (1 << 64) - 1), reserved=st.integers(0, (1 << 64) - 1)),
    st.data())
def test_pack_then_parse_round_trips(t, data):
    t.signature = data.draw(st.binary(min_size=64, max_size=64))
    try:
        packed = T.pack_trailer(t)
    except OtaError:
        return                                        # over the 4 KiB trailer sector
    back = T.parse_trailer(packed)
    assert back == t
    assert T.pack_trailer(back) == packed


@given(F.mutated(_GOOD))
def test_no_edit_to_a_genuine_trailer_is_accepted_unless_it_is_inert(bad):
    ok, _reason = verify_image(_BODY, bad, F.roots())
    if ok:
        # Only bytes nothing reads may differ: whatever follows the CRC.
        assert bad[:len(_GOOD)] == _GOOD


@given(F.mutated(_BODY))
def test_no_edit_to_a_genuine_body_is_accepted(bad):
    ok, _reason = verify_image(bad, _GOOD, F.roots())
    assert not ok


# --- the device's slot evaluation: the same attacks, through boot.py -------------------------

_STATUS = None


def _status():
    global _STATUS
    if _STATUS is None:
        from openmv_ota.ota import status as host_status
        _STATUS = host_status.build_status_sector(4096, pending=True, tried=False,
                                                  confirmed=False)
    return _STATUS


_ERASED = b"\xff" * 64       # the erased flash past a body: what a slot read returns there


def _evaluate(body, trailer):
    try:
        t, _consume = B.evaluate_slot(body + _ERASED, _status(), trailer, 0, 0x1234,
                                      F.roots(), F.device_verify)
        return t
    except B.OtaReject:
        return None


def test_the_genuine_slot_boots():
    assert _evaluate(_BODY, _GOOD) is not None


@given(F.mutated(_GOOD))
def test_boot_rejects_every_edit_to_a_genuine_trailer_unless_inert(bad):
    t = _evaluate(_BODY, bad)
    if t is not None:
        assert bad[:len(_GOOD)] == _GOOD


@given(F.mutated(_BODY))
def test_boot_rejects_every_edit_to_a_genuine_body(bad):
    t = _evaluate(bad, _GOOD)
    if t is not None:
        # boot.py hashes exactly body_size bytes OF THE SLOT, so what counts is the slot as read:
        # an edit past body_size is not an edit to the image, and neither is deleting a trailing
        # 0xFF byte (the fuzzer's find) -- erased flash reads back the very same byte.
        slot = bad + _ERASED
        assert slot[:len(_BODY)] == _BODY
        assert hashlib.sha256(slot[:t.body_size]).digest() == t.body_sha256

"""Fuzz the OCDL delta codec: the host reference (``openmv_ota.ota.delta``) and the device's
streamed applier in the installer (``_PatchReader`` + ``_delta_stream``).

A delta is the one artifact the device consumes BEFORE it can check anything about it: the
reconstructed image is sha256-verified only after every byte has been produced, so the patch's
own length varints and seeks steer the applier while they are still unverified."""

from __future__ import annotations

import io

from hypothesis import given
from hypothesis import strategies as st

from openmv_ota.ota import delta as D
from tests.fuzz import fuzzlib as F

INST = F.load_installer()


def device_apply(base: bytes, patch: bytes, chunk: int = 64) -> bytes:
    """Run the installer's streamed applier to completion, the way ``run()`` wires it: the base
    is read through a guard that refuses anything past the base region (``base_read``)."""
    limit = len(base)
    mv = memoryview(base)

    def base_read(off, n):
        if off + n > limit:
            raise ValueError("delta reads past the base body region")
        return mv[off:off + n]

    out = bytearray()
    for piece in INST._delta_stream(INST._PatchReader(io.BytesIO(patch)), base_read, chunk):
        out += piece
    return bytes(out)


_blob = st.binary(max_size=600)


@st.composite
def related_images(draw):
    """A base and a target that share most of their bytes, so make_delta finds real matches
    (diff regions with nonzero bytes, seeks both ways) instead of one big literal."""
    base = draw(st.binary(min_size=0, max_size=900))
    target = bytearray(base)
    for _ in range(draw(st.integers(0, 6))):
        op = draw(st.sampled_from(("flip", "insert", "delete", "move")))
        n = len(target)
        if op == "flip" and n:
            i = draw(st.integers(0, n - 1))
            target[i] ^= draw(st.integers(1, 255))
        elif op == "insert":
            i = draw(st.integers(0, n))
            target[i:i] = draw(st.binary(max_size=40))
        elif op == "delete" and n:
            i = draw(st.integers(0, n - 1))
            del target[i:i + draw(st.integers(1, 64))]
        elif op == "move" and n > 64:
            i = draw(st.integers(0, n - 64))
            blk = bytes(target[i:i + 64])
            del target[i:i + 64]
            target += blk
    return base, bytes(target)


@given(related_images())
def test_make_then_apply_round_trips_on_host_and_device(pair):
    base, target = pair
    patch = D.make_delta(base, target)
    assert D.apply_delta(base, patch) == target
    assert D.target_size(patch) == len(target)
    assert device_apply(base, patch) == target
    s = D.summarize(patch)
    assert s["extra_bytes"] + s["diff_bytes"] == len(target)


@given(_blob, _blob)
def test_unrelated_images_round_trip(base, target):
    patch = D.make_delta(base, target)
    assert D.apply_delta(base, patch) == target
    assert device_apply(base, patch, chunk=7) == target


# --- arbitrary / malformed patches --------------------------------------------------------------

@st.composite
def patches(draw):
    """Structured-but-hostile OCDL patches: a real header and op framing (so the fuzzer gets
    past the magic), with lengths and seeks that disagree with the payload and the base."""
    out = bytearray(draw(st.just(D.MAGIC) | st.binary(min_size=4, max_size=4)))
    D._write_uvarint(out, draw(st.integers(0, 2000) | st.integers(0, 1 << 70)))
    for _ in range(draw(st.integers(0, 6))):
        e = draw(st.integers(0, 80))
        d = draw(st.integers(0, 80))
        D._write_uvarint(out, e)
        D._write_uvarint(out, d)
        D._write_svarint(out, draw(st.integers(-300, 300)))
        out += draw(st.binary(min_size=0, max_size=e + d))
    if draw(st.booleans()):
        out += draw(st.binary(max_size=20))
    return bytes(out)


_any_patch = patches() | st.binary(max_size=300) | st.binary(max_size=40).map(
    lambda b: D.MAGIC + b)


def host_apply(base, patch):
    try:
        return D.apply_delta(base, patch)
    except D.OtaError:
        return None


@given(st.binary(max_size=400), _any_patch)
def test_host_codec_returns_or_raises_ota_error(base, patch):
    host_apply(base, patch)
    for f in (D.target_size, D.summarize):
        try:
            f(patch)
        except D.OtaError:
            pass

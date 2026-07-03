"""Signed OTA update manifest — the descriptor a device fetches *before* it erases.

The manifest is the pre-flight authority: it names the update's identity (board,
version, the reconstructed-image digest) and the available **representations** (a
full image, and/or a delta against the golden), all under one ECDSA signature
verified with the same trusted keys and primitives as an image trailer. The device
verifies it, checks the device-relative fields (board, anti-rollback floor,
platform) and picks a representation — all while ``/rom`` is still intact, so a
wrong-board / rolled-back / malformed update is rejected without erasing anything.
The downloaded image's own trailer remains the authoritative check on boot; the
manifest just lets the device fail fast and choose the cheapest transport.

Layout mirrors :mod:`openmv_ota.ota.trailer` (little-endian)::

    [ header (HEADER_SIZE) ][ json_body (body_size) ][ signature (sig_size) ][ crc32 ]
    \\______ signed region: header || json_body ______/

The **signed region** is exactly ``header || json_body`` — the signer signs those
bytes and the verifier hashes the identical stored bytes, so there is no
JSON-canonicalisation pitfall. ``key_id``/``sig_alg`` sit in the fixed header so a
verifier can select the key and algorithm before trusting anything; everything the
device acts on (product_id, payload_version, representations, the result digest) lives
in the signed JSON body.

The JSON body schema (``schema`` == ``SCHEMA``)::

    product_id              int     device cross-flash guard (0 = any)
    product               str     human board/product name (informational)
    version               str     new image's MAJOR.MINOR.PATCH (informational)
    payload_version       int     encoded uint32 -- anti-rollback compare
    min_platform_version  int     firmware floor (0 = none)
    size                  int     reconstructed full-slot image size, bytes
    sha256                str     sha256 (hex) of the reconstructed full-slot image
    representations       list    transports that all reconstruct that one image:
        format            str     "full" | "bsdiff"
        url               str     absolute https:// URL of the (gzipped) artifact
        size              int     compressed artifact size, bytes (for picking smallest)
        base_payload_version int  ("bsdiff" only) the golden this delta applies against

``size``/``sha256`` are top-level because every representation reconstructs the
*same* image; the per-representation ``size`` is the download cost the device
minimises over.
"""

from __future__ import annotations

import binascii
import json
import struct
from dataclasses import dataclass

from .algorithms import algorithm_for
from .errors import OtaError

MAGIC = b"OMVM"            # OTA update manifest (cf. OMVR image / OMVF firmware)
HEADER_VERSION = 1
SCHEMA = 1
DELTA_FORMAT = "ocdl"      # representation["format"] for an openmv_ota.ota.delta patch

# magic, header_version, body_size, sig_size, key_id, sig_alg (the lone signed "i").
HEADER_STRUCT = "<4sIIIIi"
HEADER_SIZE = struct.calcsize(HEADER_STRUCT)            # 24
_BODY_SIZE_OFFSET = struct.calcsize("<4sI")             # magic, header_version => 8
CRC_SIZE = 4
# Generous cap: a manifest is a little JSON + one signature, never near this.
MANIFEST_SZ = 8192


@dataclass
class Manifest:
    """A decoded manifest: the signed JSON ``body`` plus the trust header fields."""

    body: dict
    key_id: int
    sig_alg: int
    signature: bytes = b""
    header_version: int = HEADER_VERSION


def _serialize_body(body: dict) -> bytes:
    """Deterministic JSON (sorted keys, compact, UTF-8) — reproducible builds;
    correctness relies on signing the stored bytes, not on canonicalisation."""
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def signed_region(data) -> bytes:
    """The exact bytes signed/verified: ``header || json_body``.

    Given a :class:`Manifest`, build that region (sig_size from the algorithm, no
    signature yet) — the signer's input. Given raw manifest bytes, slice
    ``[0 : HEADER_SIZE + body_size]`` using the bounds-checked ``body_size``."""
    if isinstance(data, Manifest):
        spec = algorithm_for(data.sig_alg)
        body_bytes = _serialize_body(data.body)
        header = struct.pack(
            HEADER_STRUCT, MAGIC, data.header_version, len(body_bytes), spec.sig_size,
            data.key_id, data.sig_alg)
        return header + body_bytes
    if len(data) < HEADER_SIZE:
        raise OtaError("manifest too small: %d < %d header bytes" % (len(data), HEADER_SIZE))
    body_size = struct.unpack_from("<I", data, _BODY_SIZE_OFFSET)[0]
    end = HEADER_SIZE + body_size
    if end > len(data):
        raise OtaError("signed region [0:%d] exceeds %d bytes" % (end, len(data)))
    return bytes(data[:end])


def pack_manifest(m: Manifest) -> bytes:
    """Serialise a complete manifest: ``header || json_body || signature || crc32``.
    The signature must already be set and match the algorithm's size."""
    region = signed_region(m)
    spec = algorithm_for(m.sig_alg)
    if len(m.signature) != spec.sig_size:
        raise OtaError(
            "signature must be %d bytes for %s, got %d"
            % (spec.sig_size, spec.name, len(m.signature)))
    body = region + m.signature
    crc = binascii.crc32(body) & 0xFFFFFFFF
    out = body + struct.pack("<I", crc)
    if len(out) > MANIFEST_SZ:
        raise OtaError("manifest is %d bytes, over the %d-byte limit" % (len(out), MANIFEST_SZ))
    return out


def parse_manifest(data: bytes) -> Manifest:
    """Decode and validate a manifest. Raises ``OtaError`` on any malformation — bad
    magic, unknown ``header_version``, an unsupported algorithm, a ``sig_size`` that
    disagrees with the algorithm, truncation, a bad CRC, or non-JSON body."""
    if len(data) < HEADER_SIZE:
        raise OtaError("manifest too small: %d < %d header bytes" % (len(data), HEADER_SIZE))
    magic, header_version, body_size, sig_size, key_id, sig_alg = struct.unpack_from(
        HEADER_STRUCT, data, 0)
    if magic != MAGIC:
        raise OtaError("bad magic %r (expected %r)" % (magic, MAGIC))
    if header_version != HEADER_VERSION:
        raise OtaError(
            "unsupported header_version %d (this codec writes %d)"
            % (header_version, HEADER_VERSION))
    spec = algorithm_for(sig_alg)
    if sig_size != spec.sig_size:
        raise OtaError("sig_size %d != %d for %s" % (sig_size, spec.sig_size, spec.name))

    body_end = HEADER_SIZE + body_size + sig_size
    if body_end + CRC_SIZE > len(data):
        raise OtaError("manifest truncated: needs %d bytes, have %d" % (body_end + CRC_SIZE, len(data)))
    crc_stored = struct.unpack_from("<I", data, body_end)[0]
    if (binascii.crc32(data[:body_end]) & 0xFFFFFFFF) != crc_stored:
        raise OtaError("crc32 mismatch (corrupt manifest)")

    body_bytes = data[HEADER_SIZE : HEADER_SIZE + body_size]
    signature = data[HEADER_SIZE + body_size : body_end]
    try:
        body = json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise OtaError("manifest body is not valid JSON: %s" % e) from None

    return Manifest(body=body, key_id=key_id, sig_alg=sig_alg,
                    signature=bytes(signature), header_version=header_version)


# --- pure policy (mirrored by the device installer, pinned by tests) ---------

def update_reject_reason(body, product_id, platform_version, rollback_floor, account_id=""):
    """The device-relative pre-flight check, applied to an *already signature-verified*
    manifest body before any download/erase. Mirrors the image trailer's checks in
    ``boot.evaluate_slot`` (board cross-flash guard, platform floor, anti-rollback), so
    the manifest rejects the same updates the image's own trailer would on boot — just
    earlier. ``account_id`` (when the device has one baked in) additionally rejects a
    manifest from a different account -- defense in depth behind the server's account
    scoping and the signature. Returns a short reason string to reject, or ``None``."""
    if body.get("schema") != SCHEMA:
        return "schema"
    if product_id and body.get("product_id", 0) != product_id:
        return "board"
    if account_id and body.get("account_id", "") != account_id:
        return "account"
    mpv = body.get("min_platform_version", 0)
    if mpv and mpv > platform_version:
        return "compat"
    if body.get("payload_version", 0) < rollback_floor:
        return "rollback"
    return None


def select_representation(body, delta_capable, golden_payload_version):
    """Pick the cheapest usable representation (smallest compressed ``size``). A
    ``full`` is always usable; a ``bsdiff`` delta is usable only if the device can apply
    deltas *and* the delta's ``base_payload_version`` matches the device's golden (BACK)
    version. Returns the chosen representation dict, or ``None`` if none is usable."""
    best = None
    for rep in body.get("representations", []):
        fmt = rep.get("format")
        if fmt == DELTA_FORMAT:
            if not delta_capable or rep.get("base_payload_version") != golden_payload_version:
                continue
        elif fmt != "full":
            continue                                    # unknown transport -- skip
        if best is None or rep.get("size", 1 << 62) < best.get("size", 1 << 62):
            best = rep
    return best

"""Host-side verification of a built OTA image (body + signed trailer).

This is the mirror of what the device's ``boot.py`` does for *authenticity and
integrity* — verify the signature over the trailer's signed region against a
trusted (non-revoked) key, and confirm the body matches the signed digest. It does
**not** check the device-relative fields (``product_id`` vs the device,
``payload_version`` anti-rollback vs the installed image, ``min_platform_version``
vs the running firmware) — those need a device, not a host. So this is the
pre-publish / CI gate: "is this image genuine and intact?"
"""

from __future__ import annotations

import hashlib

from .algorithms import algorithm_for
from .errors import OtaError
from .sign import verify_region


def _roots_map(roots) -> dict:
    """``roots`` as ``{root_id: pubkey bytes}``: a ``RootKey`` list (``keys/root_keys.json``)
    or an already-built map."""
    if isinstance(roots, dict):
        return roots
    from .keys import root_keys_map
    return root_keys_map(list(roots))


def verify_image(body: bytes, trailer_bytes: bytes, roots) -> tuple[bool, str]:
    """Verify ``body`` against its ``trailer_bytes`` using the project's ``roots`` (a
    ``RootKey`` list or ``{root_id: pubkey bytes}``). Two signatures, the way a camera checks
    them: the root over the trailer's certificate, then the certified key over the trailer.
    Returns ``(ok, reason)``."""
    from . import certificate as certificate_mod
    from . import components
    from .keys import public_key_from_hex
    from .trailer import parse_trailer, signed_region

    try:
        t = parse_trailer(trailer_bytes)
    except OtaError as e:
        return False, "invalid trailer: %s" % e
    try:
        cert = certificate_mod.verify(t.certificate, _roots_map(roots))
    except OtaError as e:
        return False, "untrusted certificate: %s" % e
    alg = algorithm_for(cert.alg)
    pub = public_key_from_hex(cert.pubkey.hex(), alg)
    if not verify_region(pub, signed_region(trailer_bytes), t.signature, alg):
        return False, "signature does not verify against key 0x%08x" % cert.key_id
    if len(body) < t.body_size:
        return False, "body is %d bytes, trailer says %d" % (len(body), t.body_size)
    if hashlib.sha256(body[:t.body_size]).digest() != t.body_sha256:
        return False, "body hash mismatch (tampered, or wrong body for this trailer)"
    reason = components.check(body, t.components)     # the blobs past the filesystem
    if reason is not None:
        return False, reason
    return True, "signed by key 0x%08x (%s, root %d, epoch %d), body sha256 OK%s" % (
        cert.key_id, alg.name, cert.root_id, cert.epoch,
        ", %d component(s) OK" % len(t.components) if t.components else "")


def verify_manifest(manifest_bytes: bytes, roots) -> tuple[bool, str]:
    """Verify a signed update manifest's authenticity -- the mirror of what the device does
    pre-flight, and the same two signatures :func:`verify_image` checks: a root in ``roots``
    (a ``RootKey`` list or ``{root_id: pubkey bytes}``) over the manifest's certificate, then
    the certified key over the signed region. Only authenticity: the device-relative fields
    (board/version/platform/stub) are :func:`openmv_ota.ota.manifest.update_reject_reason`'s
    job, and a retired certificate (epoch) is the camera's floor to judge. Returns
    ``(ok, reason)``."""
    from . import certificate as certificate_mod
    from .keys import public_key_from_hex
    from .manifest import parse_manifest, signed_region

    try:
        m = parse_manifest(manifest_bytes)
    except OtaError as e:
        return False, "invalid manifest: %s" % e
    try:
        cert = certificate_mod.verify(m.certificate, _roots_map(roots))
    except OtaError as e:
        return False, "untrusted certificate: %s" % e
    if cert.alg != m.sig_alg:
        return False, "algorithm mismatch: manifest is %d, certified key 0x%08x is %d" % (
            m.sig_alg, cert.key_id, cert.alg)
    alg = algorithm_for(m.sig_alg)
    pub = public_key_from_hex(cert.pubkey.hex(), alg)
    if not verify_region(pub, signed_region(manifest_bytes), m.signature, alg):
        return False, "signature does not verify against key 0x%08x" % cert.key_id
    return True, "signed by key 0x%08x (%s, root %d, epoch %d)" % (
        cert.key_id, alg.name, cert.root_id, cert.epoch)

"""Single-file backup of a project's private signing keys.

Losing ``keys/private/`` means you can never sign an update for the fielded fleet again, so
the tool keeps a one-file archive you stash off-machine. The PEMs inside are archived **as
they sit on disk** -- already passphrase-encrypted at rest -- so the archive adds no second
encryption layer (a wrap over encrypted bytes protected nothing) and restore needs no
passphrase. What it does add is integrity: a magic tag plus a SHA-256 over the payload, so a
bit-rotted or truncated archive fails loudly at restore rather than surfacing at your next
signing. A ``--dev`` project is refused a backup outright -- its cached throwaway passphrase
lives beside the keys, making any copy effectively plaintext, and dev keys are disposable.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re

from .errors import ProjectError

MAGIC = b"OMVKB2"
_HASH_LEN = 32
# What a backup holds: the key PEMs (``ota-0100.pem``, ``root-0.pem``, ...) and the payload
# keys. The SHA-256 below is integrity against rot, not authenticity -- anyone can make an
# archive that passes it -- so a name is checked before it is ever used as a path.
_NAME = re.compile(r"^(?:[a-z]+-[0-9a-f]{1,16}\.pem|payload-keys\.bin)$")


def pack_keys(pems: dict[str, bytes]) -> bytes:
    """Archive ``{filename: pem-bytes}`` -> ``MAGIC || sha256(payload) || payload``."""
    if not pems:
        raise ProjectError("no private keys to back up")
    payload = json.dumps({name: base64.b64encode(pem).decode("ascii")
                          for name, pem in pems.items()}).encode("utf-8")
    return MAGIC + hashlib.sha256(payload).digest() + payload


def unpack_keys(blob: bytes) -> dict[str, bytes]:
    """Recover ``{filename: pem-bytes}`` from an archive. Raises ``ProjectError`` on a bad
    magic, truncation, or an integrity mismatch (recovery fails loudly, never silently)."""
    head = len(MAGIC) + _HASH_LEN
    if len(blob) < head or bytes(blob[:len(MAGIC)]) != MAGIC:
        raise ProjectError("not an openmv-ota key backup")
    digest, payload = bytes(blob[len(MAGIC):head]), bytes(blob[head:])
    if hashlib.sha256(payload).digest() != digest:
        raise ProjectError("corrupt key backup (integrity check failed)")
    try:
        entries = json.loads(payload)
        if not isinstance(entries, dict):
            raise ValueError("not a table of files")
        for name, b64 in entries.items():
            if not _NAME.match(name) or not isinstance(b64, str):
                raise ValueError("%r is not a key file's name" % name)
        return {name: base64.b64decode(b64, validate=True) for name, b64 in entries.items()}
    except ValueError as e:
        raise ProjectError("not a key backup this tool wrote: %s" % e) from None

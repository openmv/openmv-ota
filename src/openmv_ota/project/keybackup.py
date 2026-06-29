"""Encrypted backup of a project's private signing keys.

Losing ``keys/private/`` means you can never sign an update for the fielded fleet again, so
the tool keeps an **encrypted** backup you can stash off-machine. The bundle is the private
PEMs, AES-encrypted (Fernet) under a key derived from a passphrase with scrypt; the salt is
stored alongside the ciphertext. No plaintext key material is ever written here -- only the
encrypted blob -- and the passphrase is never stored. ``decrypt_keys`` is the recovery path.
"""

from __future__ import annotations

import base64
import json
import os

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .errors import ProjectError

MAGIC = b"OMVKB1"
_SALT_LEN = 16
_SCRYPT_N = 2 ** 14   # interactive cost; fine for a once-in-a-while backup


def _fernet(passphrase: str, salt: bytes) -> Fernet:
    key = Scrypt(salt=salt, length=32, n=_SCRYPT_N, r=8, p=1).derive(passphrase.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_keys(pems: dict[str, bytes], passphrase: str, salt: bytes | None = None) -> bytes:
    """Encrypt ``{filename: pem-bytes}`` under ``passphrase`` -> ``MAGIC || salt || token``.
    ``salt`` is generated randomly unless supplied (tests pass a fixed one)."""
    if not pems:
        raise ProjectError("no private keys to back up")
    salt = salt if salt is not None else os.urandom(_SALT_LEN)
    payload = json.dumps({name: base64.b64encode(pem).decode("ascii")
                          for name, pem in pems.items()}).encode("utf-8")
    return MAGIC + salt + _fernet(passphrase, salt).encrypt(payload)


def decrypt_keys(blob: bytes, passphrase: str) -> dict[str, bytes]:
    """Recover ``{filename: pem-bytes}`` from a backup blob. Raises ``ProjectError`` on a bad
    magic/truncation or a wrong passphrase (so recovery fails loudly, never silently)."""
    head = len(MAGIC) + _SALT_LEN
    if len(blob) < head or bytes(blob[:len(MAGIC)]) != MAGIC:
        raise ProjectError("not an openmv-ota key backup")
    salt, token = bytes(blob[len(MAGIC):head]), bytes(blob[head:])
    try:
        payload = _fernet(passphrase, salt).decrypt(token)
    except InvalidToken:
        raise ProjectError("wrong passphrase or corrupt key backup") from None
    return {name: base64.b64decode(b64) for name, b64 in json.loads(payload).items()}

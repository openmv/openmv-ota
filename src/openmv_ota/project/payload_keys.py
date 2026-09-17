"""The project's payload keys: one per board target, encrypted at rest.

These are the keys :mod:`openmv_ota.ota.payload` wraps content keys under, and the
constants ``build firmware`` bakes into each board's firmware. They are minted when
the project is created and never typed, pasted or chosen by anyone -- the point of
the feature is that a project gets confidentiality without acquiring a key to look
after.

**Losing this file is losing the fleet's updates.** A camera can only install what
its baked key can unwrap, so a project that cannot reproduce its board key cannot
publish an installable release for the boards already in the field -- only new
firmware would fix it, which is a hands-on visit. So the file is archived by
``project keys backup`` alongside the signing PEMs, and for the same reason it is
encrypted at rest: it lives in a repo-shaped directory next to code, and the one
thing that must never happen is a board key reaching a bucket, a CI log, or a
laptop backup in the clear.

The container, all one file::

    MAGIC || salt(16) || iv(16) || mac(32) || ciphertext

scrypt(passphrase, salt) gives 64 bytes: the first 32 encrypt (AES-256-CBC,
PKCS#7), the last 32 authenticate. Encrypt-then-MAC over ``salt || iv ||
ciphertext``, checked before a single byte is decrypted, so a truncated or edited
file fails as a wrong file rather than as strange key material. The passphrase is
the project's existing one -- the same prompt, file or env var that unlocks the
signing keys, resolved by :mod:`openmv_ota.project.passphrase`.
"""

from __future__ import annotations

import hmac
import json
import secrets
from hashlib import sha256
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from cryptography.hazmat.primitives.padding import PKCS7

from openmv_ota.ota.payload import new_key

from .errors import ProjectError

MAGIC = b"OMVPK1"
FILE_NAME = "payload-keys.bin"
_SALT = 16
_IV = 16
_MAC = 32
# scrypt at the interactive end of the scale: this runs on every build and every
# publish, and the secret behind it is the project passphrase that already guards
# the signing keys.
_N, _R, _P = 2 ** 14, 8, 1


def path_for(private_keys_dir: str | Path) -> Path:
    return Path(private_keys_dir) / FILE_NAME


def _derive(passphrase: str, salt: bytes) -> tuple[bytes, bytes]:
    dk = Scrypt(salt=salt, length=64, n=_N, r=_R, p=_P).derive(passphrase.encode("utf-8"))
    return dk[:32], dk[32:]


def _dump(keys: dict[str, dict[int, bytes]], passphrase: str) -> bytes:
    salt, iv = secrets.token_bytes(_SALT), secrets.token_bytes(_IV)
    enc_key, mac_key = _derive(passphrase, salt)
    body = json.dumps({board: {str(kid): key.hex() for kid, key in sorted(per.items())}
                       for board, per in sorted(keys.items())}).encode("utf-8")
    padder = PKCS7(128).padder()
    enc = Cipher(algorithms.AES(enc_key), modes.CBC(iv)).encryptor()
    ciphertext = enc.update(padder.update(body) + padder.finalize()) + enc.finalize()
    mac = hmac.new(mac_key, salt + iv + ciphertext, sha256).digest()
    return MAGIC + salt + iv + mac + ciphertext


def _load(blob: bytes, passphrase: str) -> dict[str, dict[int, bytes]]:
    head = len(MAGIC) + _SALT + _IV + _MAC
    if len(blob) < head or bytes(blob[:len(MAGIC)]) != MAGIC:
        raise ProjectError("not an openmv-ota payload key file")
    salt = bytes(blob[len(MAGIC):len(MAGIC) + _SALT])
    iv = bytes(blob[len(MAGIC) + _SALT:len(MAGIC) + _SALT + _IV])
    mac, ciphertext = bytes(blob[len(MAGIC) + _SALT + _IV:head]), bytes(blob[head:])
    enc_key, mac_key = _derive(passphrase, salt)
    if not hmac.compare_digest(hmac.new(mac_key, salt + iv + ciphertext, sha256).digest(), mac):
        # One message for both cases on purpose: from the outside a wrong passphrase
        # and an edited file are the same event -- this file did not open.
        raise ProjectError("the payload key file did not open: wrong passphrase, or the "
                           "file has been altered")
    dec = Cipher(algorithms.AES(enc_key), modes.CBC(iv)).decryptor()
    unpadder = PKCS7(128).unpadder()
    body = unpadder.update(dec.update(ciphertext) + dec.finalize()) + unpadder.finalize()
    return {board: {int(kid): bytes.fromhex(key) for kid, key in per.items()}
            for board, per in json.loads(body).items()}


def write(private_keys_dir: str | Path, keys: dict[str, dict[int, bytes]],
          passphrase: str) -> Path:
    """Write the whole set. Callers mint or rotate through the helpers below rather
    than assembling this themselves."""
    out = path_for(private_keys_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(_dump(keys, passphrase))
    return out


def read(private_keys_dir: str | Path, passphrase: str) -> dict[str, dict[int, bytes]]:
    """``{board: {key_id: key}}``. Raises ``ProjectError`` if the file is missing --
    a project whose payload keys are gone cannot publish anything its fielded
    cameras could install, and that is worth saying at once rather than at the end
    of a build."""
    blob = path_for(private_keys_dir)
    if not blob.exists():
        raise ProjectError(
            "no payload keys in %s -- every OTA project is created with them, so this "
            "project's are lost or were never copied here. Restore them with `openmv-ota "
            "project keys restore <file>` (they are in the key backup); without them a camera in "
            "the field cannot decrypt anything you publish." % blob.parent, exit_code=1)
    return _load(blob.read_bytes(), passphrase)


def mint(private_keys_dir: str | Path, boards: list[str], passphrase: str) -> Path:
    """Mint the first key for each board of a new project."""
    return write(private_keys_dir, {board: {1: new_key()} for board in boards}, passphrase)


def ensure_boards(private_keys_dir: str | Path, boards: list[str], passphrase: str) -> list[str]:
    """Mint keys for boards added to the project since it was created, returning the
    board names that gained one. A board added later is a normal thing to do and must
    not need a key ceremony."""
    keys = read(private_keys_dir, passphrase)
    added = [b for b in boards if b not in keys]
    for board in added:
        keys[board] = {1: new_key()}
    if added:
        write(private_keys_dir, keys, passphrase)
    return added


def rotate(private_keys_dir: str | Path, board: str, passphrase: str) -> int:
    """Mint the next key for one board and return its id.

    The old key is KEPT, and both are baked into the next firmware: a camera can
    only install what it can unwrap, so the fleet has to be carried across on the
    old key while the new firmware reaches it. Dropping the old key is a separate,
    later decision -- see ``retire``."""
    keys = read(private_keys_dir, passphrase)
    if board not in keys:
        raise ProjectError("%s is not a board in this project" % board, exit_code=1)
    key_id = max(keys[board]) + 1
    keys[board][key_id] = new_key()
    write(private_keys_dir, keys, passphrase)
    return key_id


def retire(private_keys_dir: str | Path, board: str, key_id: int, passphrase: str) -> None:
    """Drop one board key. Every camera still running firmware built with it stops
    being able to install anything, so this is only for after a rotation has reached
    the whole fleet."""
    keys = read(private_keys_dir, passphrase)
    if key_id not in keys.get(board, {}):
        raise ProjectError("%s has no payload key %d" % (board, key_id), exit_code=1)
    if len(keys[board]) == 1:
        raise ProjectError(
            "%s has only this one payload key -- retiring it would leave nothing to "
            "encrypt with. Rotate first." % board, exit_code=1)
    del keys[board][key_id]
    write(private_keys_dir, keys, passphrase)

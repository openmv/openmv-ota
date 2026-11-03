"""An account's pinned roots: the trust anchor the server checks desk-key chains against.

A camera embeds its project's roots; the server embeds nothing, so a project tells the server
its roots once (``openmv-ota project keys roots push``) and the account is PINNED to them: a
desk key is recorded only if its certificate chains to one of them. Pinning is set-once. Replacing
the roots takes a *statement* -- the new set, signed by one of the roots the account already
has -- so the admin token alone can neither set a stranger's roots over a pinned account nor
swap them later. A fresh account, which has no roots to sign with, takes its first set from
whoever holds its admin token: that is the one moment the token stands in for the root, and it
is why a project pushes its roots as soon as the account exists.

The statement is JSON signed as exact bytes (like a registration record): the same canonical
encoding on both sides, verified as ES256 ``R||S`` hex over those bytes.
"""

from __future__ import annotations

import json

from .algorithms import ES256, algorithm_for
from .errors import OtaError
from .keys import public_key_from_hex
from .sign import verify_region

VERSION = 1
_ALG = algorithm_for(ES256)
MAX_ROOTS = 4                      # a project ships two; room for a rotation that overlaps


class RootsError(OtaError):
    """A roots set or statement that does not pass, with the reason."""


def check_roots(roots) -> list[dict]:
    """Normalise ``[{"root_id": int, "pubkey": hex}, ...]``: ids distinct and small, keys
    uncompressed P-256 points (lower-cased). Raises :class:`RootsError`."""
    if not isinstance(roots, list) or not roots or len(roots) > MAX_ROOTS:
        raise RootsError("an account pins between 1 and %d roots" % MAX_ROOTS)
    out, seen = [], set()
    for r in roots:
        try:
            root_id, pubkey = int(r["root_id"]), str(r["pubkey"]).lower()
        except (KeyError, TypeError, ValueError):
            raise RootsError("each root is a root_id and a pubkey") from None
        if isinstance(r["root_id"], bool) or not 0 <= root_id < 16 or root_id in seen:
            raise RootsError("root ids are distinct small integers")
        try:
            if not pubkey.startswith("04"):
                raise ValueError
            public_key_from_hex(pubkey, _ALG)
        except (ValueError, OtaError):
            raise RootsError("root %d's key isn't an uncompressed P-256 point" % root_id) from None
        seen.add(root_id)
        out.append({"root_id": root_id, "pubkey": pubkey})
    return sorted(out, key=lambda r: r["root_id"])


def statement_bytes(account: str, roots) -> bytes:
    """The bytes a root signs to replace an account's roots with ``roots``."""
    body = {"v": VERSION, "account": account, "roots": check_roots(roots)}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def sign_statement(account: str, roots, signer) -> dict:
    """``{"signed_by": root_id, "signature": hex}``: ``signer`` is a root's
    ``ota.signer.Signer`` whose ``key_id`` is that root's id."""
    return {"signed_by": int(signer.key_id),
            "signature": bytes(signer.sign(statement_bytes(account, roots))).hex()}


def verify_statement(account: str, roots, statement, current) -> list[dict]:
    """The normalised new ``roots`` if ``statement`` is a current root's signature over exactly
    the statement for them (``current`` is the account's pinned set). Raises :class:`RootsError`
    saying why not."""
    try:
        signed_by, signature = int(statement["signed_by"]), str(statement["signature"])
    except (KeyError, TypeError, ValueError):
        raise RootsError("a statement is signed_by (a current root id) and a signature") from None
    by_id = {r["root_id"]: r["pubkey"] for r in current}
    if signed_by not in by_id:
        raise RootsError("root %d is not one of the account's current roots" % signed_by)
    region = statement_bytes(account, roots)
    try:
        good = verify_region(public_key_from_hex(by_id[signed_by], _ALG), region,
                             bytes.fromhex(signature), _ALG)
    except ValueError:
        good = False
    if not good:
        raise RootsError("the statement is not signed by root %d" % signed_by)
    return check_roots(roots)

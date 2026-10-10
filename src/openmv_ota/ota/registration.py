"""Camera registration records: "this camera's two public keys belong to this account".

A desk (``openmv-ota flash factory``) checks that a camera holds its keys, then signs one of these
with the project's **desk key** and uploads it. Everything that later trusts a camera's keys --
the server checking a signed check-in, keykeeper wrapping an update for one camera -- checks the
desk's signature first, so a server that has been broken into can store and serve these records
but cannot make one: that takes a desk key, which only the project holds.

The record is JSON, signed as the exact bytes it was written as: it is stored and passed on
byte-for-byte, never re-encoded, because two JSON encoders can write the same data differently
and the signature covers the bytes, not the meaning. The signature is raw ``R||S`` (ES256, the
same as image signatures), hex.

    {"v": 1, "account": "acct_...", "device_id": "OPENMV_N6:0036...", "board": "OPENMV_N6",
     "identity": "04...", "exchange": "04...", "desk": 1, "issued": "2026-10-10T12:00:00Z"}

``identity`` signs (check-ins); ``exchange`` only does ECDH (an update's lockbox). Both are
uncompressed P-256 points, 65 bytes. ``desk`` is the id of the desk key that signed the record.

Desk keys expire: a record is good if its desk key had not expired when it was ISSUED and has not
been revoked since. A desk that stops renewing its key stops registering cameras, while every camera
it registered stays registered. Expiry is housekeeping (it retires forgotten stations), not the
defence against a stolen key: the server only accepts a record from a key that is unexpired NOW
(:func:`check_upload`), and a key that may have leaked is revoked, which takes effect at once.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from .algorithms import ES256, algorithm_for
from .errors import OtaError
from .keys import public_key_from_hex
from .sign import verify_region

VERSION = 1
_ALG = algorithm_for(ES256)
_FIELDS = ("v", "account", "device_id", "board", "identity", "exchange", "desk", "issued")
_SKEW_S = 300                                    # a desk's clock may run ahead of the checker's
_UPLOAD_WINDOW_S = 3600                          # a record is uploaded as soon as it's made


class RegistrationError(OtaError):
    """A registration record that doesn't verify, with the reason."""


@dataclass(frozen=True)
class DeskKey:
    """A desk key as its verifiers know it: the public half, and when it stops registering."""
    key_id: int
    public: str                                  # uncompressed P-256 point, hex
    expires: str                                 # ISO 8601, UTC
    revoked: bool = False


@dataclass(frozen=True)
class Registration:
    """A verified record's contents."""
    account: str
    device_id: str
    board: str
    identity: str
    exchange: str
    desk: int
    issued: str


def _utc(text: str) -> datetime:
    when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if when.tzinfo is None:
        raise ValueError("no time zone")
    return when.astimezone(timezone.utc)


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _point(hex_point: str, name: str) -> None:
    try:
        raw = bytes.fromhex(hex_point)
        if len(raw) != 65 or raw[0] != 4:
            raise ValueError
        public_key_from_hex(hex_point, _ALG)          # on the curve
    except (ValueError, TypeError, OtaError):
        raise RegistrationError("the %s key isn't a P-256 public key" % name) from None


def check_public_key(hex_point: str, name: str = "desk") -> None:
    """Raise :class:`RegistrationError` unless ``hex_point`` is an uncompressed P-256 point."""
    _point(hex_point, name)


def build(*, account: str, device_id: str, board: str, identity: str, exchange: str,
          desk: int, issued: datetime) -> bytes:
    """A record's bytes, ready to sign. Keys are compared case-insensitively, so they're
    lower-cased here once."""
    identity, exchange = identity.lower(), exchange.lower()
    _point(identity, "identity")
    _point(exchange, "exchange")
    body = {"v": VERSION, "account": account, "device_id": device_id, "board": board,
            "identity": identity, "exchange": exchange, "desk": desk, "issued": _iso(issued)}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def sign(record: bytes, signer) -> str:
    """The desk's signature over ``record``: raw ``R||S``, hex. ``signer`` is the desk key's
    ``ota.signer.Signer`` (encrypted PEM, PKCS#11 or KMS, like any other project key)."""
    return signer.sign(record).hex()


def verify(record: bytes, signature: str, desk_keys, *, account: str | None = None) -> Registration:
    """The record's contents, if ``signature`` is a desk key's over exactly ``record``, that key
    was unexpired when the record was issued and isn't revoked, and (given ``account``) the
    record is that account's. Raises :class:`RegistrationError` saying why not."""
    try:
        body = json.loads(record)
    except ValueError:
        raise RegistrationError("the record isn't JSON") from None
    if not isinstance(body, dict) or sorted(body) != sorted(_FIELDS):
        raise RegistrationError("the record's fields aren't a version-1 record's")
    if body["v"] != VERSION:
        raise RegistrationError("record version %r: this needs newer software" % (body["v"],))
    if not all(isinstance(body[f], str) for f in _FIELDS if f not in ("v", "desk")) \
            or not isinstance(body["desk"], int) or isinstance(body["desk"], bool):
        raise RegistrationError("the record's fields have the wrong types")
    _point(body["identity"], "identity")
    _point(body["exchange"], "exchange")
    if account is not None and body["account"] != account:
        raise RegistrationError("the record is for another account")
    desk = {k.key_id: k for k in desk_keys}.get(body["desk"])
    if desk is None:
        raise RegistrationError("desk key %d isn't one of this account's" % body["desk"])
    if desk.revoked:
        raise RegistrationError("desk key %d is revoked" % desk.key_id)
    try:
        issued = _utc(body["issued"])
    except ValueError:
        raise RegistrationError("the record's issue time isn't a UTC timestamp") from None
    if issued >= _utc(desk.expires):
        raise RegistrationError("desk key %d had expired when the record was issued" % desk.key_id)
    try:
        good = verify_region(public_key_from_hex(desk.public, _ALG), record,
                             bytes.fromhex(signature), _ALG)
    except ValueError:
        good = False
    if not good:
        raise RegistrationError("the desk's signature doesn't match the record")
    return Registration(account=body["account"], device_id=body["device_id"],
                        board=body["board"], identity=body["identity"],
                        exchange=body["exchange"], desk=body["desk"], issued=body["issued"])


def check_upload(reg: Registration, desk_keys, now: datetime) -> None:
    """The server's extra checks when a record is UPLOADED: its desk key must be unexpired now,
    not only when the record says it was issued, and the record must be fresh (issued within
    ``_UPLOAD_WINDOW_S`` of ``now``, allowing a little clock skew). Together they stop a desk key
    that has expired -- or been stolen after expiring -- from registering cameras with a
    back-dated record. A stolen key that is still current is what revocation is for."""
    desk = {k.key_id: k for k in desk_keys}[reg.desk]
    if now >= _utc(desk.expires):
        raise RegistrationError("desk key %d has expired; renew it on the desk" % desk.key_id)
    age = now.timestamp() - _utc(reg.issued).timestamp()
    if age < -_SKEW_S or age > _UPLOAD_WINDOW_S:
        raise RegistrationError("the record wasn't issued just now (check the desk's clock)")

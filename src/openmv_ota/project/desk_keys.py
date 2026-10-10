"""The project's desk keys: what registers a camera's keys to the account.

A desk is wherever cameras are first flashed -- your bench, a factory station. ``flash factory``
checks that a camera holds its keys, then signs a registration record with a desk key
(:mod:`openmv_ota.ota.registration`). Everything that later trusts a camera checks that
signature, so a desk key is what a broken-into server would need, and can't get.

Each desk gets its own key, so one can be revoked without touching the others, and each key
EXPIRES (90 days by default) and is renewed with one command. A forgotten station simply stops
registering cameras; the cameras it registered stay registered.

    keys/desk_keys.json            public, committed: id, label, public key, created, expires,
                                   revoked, and (optionally) the key's HSM/KMS backend record
    keys/private/desk-0001.pem     the private key, encrypted with the project passphrase
                                   (absent when the key lives in an HSM or KMS)

A desk key never signs firmware: the image-signing keys stay away from a factory floor, where a
leaked desk key costs fake registrations, not malicious firmware.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from openmv_ota.ota.algorithms import ES256, algorithm_for
from openmv_ota.ota.keys import generate_private_key, private_key_pem, public_point_hex
from openmv_ota.ota.registration import DeskKey
from openmv_ota.ota.signer import build_signer

from .errors import ProjectError

FILE_NAME = "desk_keys.json"
ROLE = "desk"
DEFAULT_DAYS = 90
_ALG = algorithm_for(ES256)


@dataclass
class DeskEntry:
    """One desk key in ``keys/desk_keys.json``."""
    key_id: int
    label: str
    pubkey: str
    created: str
    expires: str
    revoked: bool = False
    backend: dict | None = field(default=None)
    alg: int = ES256                             # what build_signer reads off an entry
    role: str = ROLE

    def to_dict(self) -> dict:
        d = {"key_id": self.key_id, "label": self.label, "pubkey": self.pubkey,
             "created": self.created, "expires": self.expires, "revoked": self.revoked}
        if self.backend:
            d["backend"] = self.backend
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "DeskEntry":
        return cls(key_id=int(d["key_id"]), label=str(d["label"]), pubkey=str(d["pubkey"]),
                   created=str(d["created"]), expires=str(d["expires"]),
                   revoked=bool(d.get("revoked", False)), backend=d.get("backend") or None)

    def public(self) -> DeskKey:
        """The key as a verifier knows it."""
        return DeskKey(self.key_id, self.pubkey, self.expires, self.revoked)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def path_for(root: str | Path) -> Path:
    return Path(root) / "keys" / FILE_NAME


def read(root: str | Path) -> list[DeskEntry]:
    """The project's desk keys, oldest first; none if it has never made one."""
    p = path_for(root)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return [DeskEntry.from_dict(d) for d in data.get("keys", [])]
    except (ValueError, KeyError, TypeError) as e:
        raise ProjectError("%s is damaged: %s" % (p, e)) from None


def _write(root: str | Path, keys: list[DeskEntry]) -> None:
    p = path_for(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"keys": [k.to_dict() for k in keys]}, indent=2) + "\n",
                 encoding="utf-8")


def _find(keys: list[DeskEntry], key_id: int) -> DeskEntry:
    for k in keys:
        if k.key_id == key_id:
            return k
    raise ProjectError("no desk key %d in this project" % key_id)


def create(root: str | Path, label: str, passphrase: str, *, days: int = DEFAULT_DAYS,
           now: datetime | None = None) -> DeskEntry:
    """A new desk key (the next id), its private half encrypted with the project passphrase."""
    if not label or not label.strip():
        raise ProjectError("a desk key needs a label (which desk it is)")
    keys = read(root)
    if any(k.label == label for k in keys if not k.revoked):
        raise ProjectError("desk %r already has a key; renew it, or revoke it first" % label)
    now = now or _now()
    key = generate_private_key(_ALG)
    entry = DeskEntry(key_id=max((k.key_id for k in keys), default=0) + 1, label=label,
                      pubkey=public_point_hex(key.public_key()), created=_iso(now),
                      expires=_iso(now + timedelta(days=days)))
    private = Path(root) / "keys" / "private"
    private.mkdir(parents=True, exist_ok=True)
    (private / ("%s-%04x.pem" % (ROLE, entry.key_id))).write_bytes(
        private_key_pem(key, passphrase))
    _write(root, keys + [entry])
    return entry


def renew(root: str | Path, key_id: int, *, days: int = DEFAULT_DAYS,
          now: datetime | None = None) -> DeskEntry:
    """Push a desk key's expiry out ``days`` from now. A revoked key stays revoked."""
    keys = read(root)
    entry = _find(keys, key_id)
    if entry.revoked:
        raise ProjectError("desk key %d is revoked; make a new one for that desk" % key_id)
    entry.expires = _iso((now or _now()) + timedelta(days=days))
    _write(root, keys)
    return entry


def revoke(root: str | Path, key_id: int) -> DeskEntry:
    """Stop trusting a desk key, at once and for good (it may have leaked). Cameras it
    registered must be registered again from another desk."""
    keys = read(root)
    entry = _find(keys, key_id)
    entry.revoked = True
    _write(root, keys)
    return entry


def current(root: str | Path, label: str | None = None, *,
            now: datetime | None = None) -> DeskEntry:
    """The key a desk registers with: the newest unrevoked, unexpired key (for ``label``, if
    given). Raises saying what to do when there is none."""
    now_iso = _iso(now or _now())
    usable = [k for k in read(root) if not k.revoked and k.expires > now_iso
              and (label is None or k.label == label)]
    if not usable:
        raise ProjectError("no usable desk key%s -- `openmv-ota project keys desk create` makes "
                           "one, `... renew` extends an expired one"
                           % ((" for %r" % label) if label else ""))
    return max(usable, key=lambda k: k.key_id)


def signer(root: str | Path, entry: DeskEntry, *, passphrase_provider=None):
    """The ``ota.signer.Signer`` for a desk key: its encrypted PEM, or its HSM/KMS backend."""
    return build_signer(entry, _ALG, private_keys_dir=Path(root) / "keys" / "private",
                        backend=entry.backend, passphrase_provider=passphrase_provider)

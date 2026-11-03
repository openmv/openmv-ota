"""The project's issuer key: the helper that certifies desk keys so the root can stay offline.

A camera trusts a signing key through a certificate its root signed. A desk key -- what registers
cameras to the account -- gets the same treatment, but desks come and go (a new bench, a factory
station) far more often than signing keys, and taking the root out of the drawer for each would
end with the root on a laptop. So a root certifies ONE issuer, once, when the project is made
(:func:`create`), and the issuer certifies every desk key after that
(:func:`certify_desk`). The server checks the whole chain -- root, issuer, desk -- against the
roots the account pinned, which is why the account's admin token alone can introduce no desk.

    keys/issuer.json                 public, committed: id, public key, its root-signed
                                     certificate, created, and (optionally) an HSM/KMS backend
    keys/private/issuer-<id>.pem     the private key, encrypted with the project passphrase
                                     (absent when the key lives in an HSM or KMS)

The issuer never signs an image or a registration: its certificate says so
(:data:`~openmv_ota.ota.certificate.KIND_ISSUER`), and a camera's parser rejects that kind.
Losing it costs nothing a camera notices: a new issuer is one root ceremony, and every desk
keeps the certificate it already has (each desk entry carries its issuer's certificate too).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from openmv_ota.ota import certificate as certificate_mod
from openmv_ota.ota.algorithms import ES256, algorithm_for
from openmv_ota.ota.errors import OtaError
from openmv_ota.ota.keys import (
    generate_private_key,
    load_private_key_pem,
    private_key_pem,
    public_point_hex,
    root_pem_name,
)
from openmv_ota.ota.signer import build_signer

from .errors import ProjectError

FILE_NAME = "issuer.json"
ROLE = "issuer"
ROOT_ID = 0                       # the root that certifies issuers (the primary)
ISSUER_LABEL_ID = 0x0200          # what an external backend is asked to name the key
_ALG = algorithm_for(ES256)


@dataclass
class IssuerEntry:
    """``keys/issuer.json``: the issuer as the project and its verifiers know it."""
    key_id: int
    pubkey: str
    cert: str                                    # packed KIND_ISSUER certificate, hex
    created: str
    backend: dict | None = field(default=None)
    alg: int = ES256                             # what build_signer reads off an entry
    role: str = ROLE

    def to_dict(self) -> dict:
        d = {"key_id": self.key_id, "pubkey": self.pubkey, "cert": self.cert,
             "created": self.created}
        if self.backend:
            d["backend"] = self.backend
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "IssuerEntry":
        return cls(key_id=int(d["key_id"]), pubkey=str(d["pubkey"]), cert=str(d["cert"]),
                   created=str(d["created"]), backend=d.get("backend") or None)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def path_for(root: str | Path) -> Path:
    return Path(root) / "keys" / FILE_NAME


def read(root: str | Path) -> IssuerEntry | None:
    """The project's issuer, or None if it has none yet."""
    p = path_for(root)
    if not p.exists():
        return None
    try:
        return IssuerEntry.from_dict(json.loads(p.read_text(encoding="utf-8")))
    except (ValueError, KeyError, TypeError) as e:
        raise ProjectError("%s is damaged: %s" % (p, e)) from None


def _write(root: str | Path, entry: IssuerEntry) -> None:
    """Record the issuer, and drop any earlier issuer's local PEM: one issuer per project, and
    a retired one signs nothing (the desks it certified carry its certificate, not its key)."""
    p = path_for(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(entry.to_dict(), indent=2) + "\n", encoding="utf-8")
    keep = "%s-%04x.pem" % (ROLE, entry.key_id) if not entry.backend else None
    for pem in (Path(root) / "keys" / "private").glob("%s-*.pem" % ROLE):
        if pem.name != keep:
            pem.unlink()


def _root_private_key(root: str | Path, passphrase: str):
    """Root 0's private key, from ``keys/private/root-0.pem`` -- the one ceremony."""
    pem = Path(root) / "keys" / "private" / root_pem_name(ROOT_ID)
    try:
        data = pem.read_bytes()
    except OSError:
        raise ProjectError("no %s in keys/private: the root certifies the issuer, and nothing "
                           "can take its place" % root_pem_name(ROOT_ID)) from None
    try:
        return load_private_key_pem(data, passphrase)
    except OtaError as e:
        raise ProjectError(str(e)) from None


def _certified(root: str | Path, passphrase: str, pubkey: str, *, backend=None) -> IssuerEntry:
    cert = certificate_mod.issue(_root_private_key(root, passphrase), ROOT_ID,
                                 certificate_mod.KIND_ISSUER, 0, bytes.fromhex(pubkey))
    return IssuerEntry(key_id=certificate_mod.key_id_of(bytes.fromhex(pubkey)), pubkey=pubkey,
                       cert=certificate_mod.pack(cert).hex(), created=_now_iso(),
                       backend=backend)


def create(root: str | Path, passphrase: str) -> IssuerEntry:
    """Mint the issuer and have root 0 certify it; the private half is encrypted with the
    project passphrase. A project has one issuer: making another is :func:`replace`."""
    if read(root) is not None:
        raise ProjectError("this project already has an issuer key (keys/issuer.json); "
                           "`openmv-ota project keys issuer replace` makes a new one")
    return replace(root, passphrase)


def replace(root: str | Path, passphrase: str) -> IssuerEntry:
    """A fresh issuer, certified by root 0, in place of any the project had. Desk keys already
    certified keep their certificates (each carries its own issuer's), so nothing registered
    stops working; desks made from now on are certified by the new issuer."""
    key = generate_private_key(_ALG)
    pubkey = public_point_hex(key.public_key())
    entry = _certified(root, passphrase, pubkey)
    private = Path(root) / "keys" / "private"
    private.mkdir(parents=True, exist_ok=True)
    (private / ("%s-%04x.pem" % (ROLE, entry.key_id))).write_bytes(private_key_pem(key, passphrase))
    _write(root, entry)
    return entry


def adopt_external(root: str | Path, passphrase: str, pubkey: str, backend: dict) -> IssuerEntry:
    """Record an issuer minted inside an HSM or KMS (``backend`` is its ``keys/backends.json``
    -shaped record) and have root 0 certify it. Used when a project moves its keys into an
    external backend: the issuer moves with the signing keys."""
    entry = _certified(root, passphrase, pubkey, backend=backend)
    _write(root, entry)
    return entry


def signer(root: str | Path, entry: IssuerEntry, *, passphrase_provider=None):
    """The ``ota.signer.Signer`` for the issuer: its encrypted PEM, or its HSM/KMS backend."""
    try:
        return build_signer(entry, _ALG, private_keys_dir=Path(root) / "keys" / "private",
                            backend=entry.backend, passphrase_provider=passphrase_provider)
    except OtaError as e:
        raise ProjectError(str(e)) from None


def certify_desk(root: str | Path, desk_pubkey: str, *, passphrase_provider=None) -> tuple[str, str]:
    """``(desk_cert, issuer_cert)``, hex: a KIND_DESK certificate over ``desk_pubkey`` signed by
    the project's issuer, with the issuer's own root-signed certificate beside it -- the pair a
    desk entry carries and the server checks as a chain. Raises ``ProjectError`` when the
    project has no issuer yet."""
    entry = read(root)
    if entry is None:
        raise ProjectError("this project has no issuer key: `openmv-ota project keys issuer "
                           "create` makes one (it needs keys/private/root-0.pem once)")
    issuer_cert = certificate_mod.parse(bytes.fromhex(entry.cert))
    cert = certificate_mod.issue_with(signer(root, entry, passphrase_provider=passphrase_provider),
                                      issuer_cert.root_id, certificate_mod.KIND_DESK, 0,
                                      bytes.fromhex(desk_pubkey))
    return certificate_mod.pack(cert).hex(), entry.cert

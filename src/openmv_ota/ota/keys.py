"""ECDSA key generation and the trusted-key set for OTA signing.

Host-side, built on ``cryptography``. Public keys are stored as the uncompressed
EC point in hex (``04 || X || Y``), which the device's mbedtls verifier reads
directly via ``mbedtls_ecp_point_read_binary``; private keys are unencrypted
PKCS#8 PEM.

``keys/trusted_keys.json`` is the committed public set the firmware build bakes
into ``TRUSTED_KEYS``. Each entry carries the key's id, COSE algorithm, role
(``ota`` / ``factory`` / ``emergency`` / …), and point — enough for the firmware
to verify and for the build to know which algorithm a given key signs with.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from .algorithms import AlgSpec, algorithm_for
from .errors import OtaError

# cryptography curve objects per COSE/mbedtls curve name (supported algs only).
_CURVES = {
    "secp256r1": ec.SECP256R1,
    "secp384r1": ec.SECP384R1,
    "secp521r1": ec.SECP521R1,
}


def curve_for(alg: AlgSpec) -> ec.EllipticCurve:
    """The ``cryptography`` curve instance for an algorithm."""
    return _CURVES[alg.curve]()


def generate_private_key(alg: AlgSpec):
    """Generate an ECDSA private key on ``alg``'s curve."""
    return ec.generate_private_key(curve_for(alg))


def public_point_hex(public_key) -> str:
    """The public key as the uncompressed EC point (``04 || X || Y``) in hex."""
    point = public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return point.hex()


def spki_to_point_hex(der: bytes) -> str:
    """The uncompressed EC point (hex) from a SubjectPublicKeyInfo DER -- what a cloud KMS
    ``GetPublicKey`` returns for an EC key."""
    try:
        pub = serialization.load_der_public_key(der)
    except (ValueError, TypeError) as e:
        raise OtaError("could not parse public key DER: %s" % e) from None
    return public_point_hex(pub)


def public_key_from_hex(point_hex: str, alg: AlgSpec):
    """Reconstruct a public key from an uncompressed-point hex string."""
    try:
        return ec.EllipticCurvePublicKey.from_encoded_point(
            curve_for(alg), bytes.fromhex(point_hex)
        )
    except ValueError as e:
        raise OtaError("invalid public point: %s" % e) from None


def private_key_pem(private_key, passphrase: str) -> bytes:
    """Serialize a private key as **encrypted** PKCS#8 PEM under ``passphrase``. There is no
    plaintext-write path: the tool never emits an unencrypted private key."""
    if not passphrase:
        raise OtaError("a passphrase is required to write a private key (keys are never plaintext)")
    return private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(passphrase.encode("utf-8")),
    )


def load_private_key_pem(data: bytes, passphrase: str | None = None):
    """Load a PKCS#8 PEM private key, decrypting with ``passphrase`` if the PEM is encrypted.
    ``TypeError`` = a passphrase was given for a plaintext PEM (or missing for an encrypted one)."""
    pw = passphrase.encode("utf-8") if passphrase else None
    try:
        return serialization.load_pem_private_key(data, password=pw)
    except (ValueError, TypeError) as e:
        raise OtaError("could not load private key: %s" % e) from None


# --- trusted_keys.json ------------------------------------------------------

TRUSTED_KEYS_NAME = "trusted_keys.json"
TRUSTED_KEYS_SCHEMA = 1


@dataclass
class TrustedKey:
    """One public key in the committed trusted-key set."""

    key_id: int
    alg: int            # COSE algorithm id
    role: str           # "ota" | "factory"
    pubkey: str         # uncompressed EC point, hex
    revoked: bool = False  # kept in the set (never deleted) but rejected; honored by
    #                        the firmware build's device reject-list and the host signer
    cert: str = ""      # the key's certificate (openmv_ota.ota.certificate), hex: what the
    #                     trailer carries so a camera that embeds only the roots trusts this key

    def to_dict(self) -> dict:
        return {"key_id": self.key_id, "alg": self.alg, "role": self.role,
                "pubkey": self.pubkey, "revoked": self.revoked, "cert": self.cert}

    @classmethod
    def from_dict(cls, d: dict) -> "TrustedKey":
        return cls(
            key_id=int(d["key_id"]), alg=int(d["alg"]), role=str(d["role"]),
            pubkey=str(d["pubkey"]), revoked=bool(d.get("revoked", False)),
            cert=str(d.get("cert", "")),
        )


def read_trusted_keys(path: Path) -> list[TrustedKey]:
    """Load the trusted-key set, or raise ``OtaError`` if it is missing/corrupt."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError:
        raise OtaError("no %s found" % TRUSTED_KEYS_NAME) from None
    except json.JSONDecodeError as e:
        raise OtaError("%s is not valid JSON: %s" % (TRUSTED_KEYS_NAME, e)) from None
    return [TrustedKey.from_dict(k) for k in data.get("keys", [])]


def write_trusted_keys(path: Path, keys: list[TrustedKey]) -> None:
    """Write the trusted-key set (committed, public)."""
    doc = {"schema": TRUSTED_KEYS_SCHEMA, "keys": [k.to_dict() for k in keys]}
    Path(path).write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


# --- root keys ----------------------------------------------------------------

ROOT_KEYS_NAME = "root_keys.json"
ROOT_KEYS_SCHEMA = 1
ROOT_COUNT = 2            # a primary (0) and a backup (1); the camera embeds both


@dataclass
class RootKey:
    """One root public key: what the camera (its boot stub and firmware) embeds. Roots sign
    certificates and nothing else; the private half stays offline."""

    root_id: int
    alg: int            # COSE algorithm id (ES256: certificates carry P-256 keys only)
    pubkey: str         # uncompressed EC point, hex

    def to_dict(self) -> dict:
        return {"root_id": self.root_id, "alg": self.alg, "pubkey": self.pubkey}

    @classmethod
    def from_dict(cls, d: dict) -> "RootKey":
        return cls(root_id=int(d["root_id"]), alg=int(d["alg"]), pubkey=str(d["pubkey"]))


def read_root_keys(path: Path) -> list[RootKey]:
    """Load the root-key set, or raise ``OtaError`` if it is missing/corrupt."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError:
        raise OtaError("no %s found" % ROOT_KEYS_NAME) from None
    except json.JSONDecodeError as e:
        raise OtaError("%s is not valid JSON: %s" % (ROOT_KEYS_NAME, e)) from None
    return [RootKey.from_dict(k) for k in data.get("roots", [])]


def write_root_keys(path: Path, roots: list[RootKey]) -> None:
    """Write the root-key set (committed, public)."""
    doc = {"schema": ROOT_KEYS_SCHEMA, "roots": [r.to_dict() for r in roots]}
    Path(path).write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def root_keys_map(roots: list[RootKey]) -> dict[int, bytes]:
    """``{root_id: raw public point}`` -- the shape the camera embeds and
    :func:`certificate.verify` takes."""
    return {r.root_id: bytes.fromhex(r.pubkey) for r in roots}


def root_pem_name(root_id: int) -> str:
    return "root-%d.pem" % root_id


def certify(root_private_key, root_id: int, keys: list[TrustedKey], *, epoch: int = 0) -> None:
    """Issue a certificate for every key in ``keys`` (in place): ``role`` picks the
    certificate's kind, so a factory key can never sign a release and an OTA key can never
    open a camera's key-provisioning window."""
    from . import certificate as certificate_mod

    kinds = {"ota": certificate_mod.KIND_OTA, "factory": certificate_mod.KIND_FACTORY}
    for k in keys:
        if k.role not in kinds:
            raise OtaError("no certificate kind for a %r key" % k.role)
        c = certificate_mod.issue(root_private_key, root_id, kinds[k.role], epoch,
                                  bytes.fromhex(k.pubkey))
        k.cert = certificate_mod.pack(c).hex()


# --- provisioning -----------------------------------------------------------

# key_id ranges, well-separated so role pools don't collide at realistic counts.
FACTORY_KEY_ID_BASE = 0x0001
OTA_KEY_ID_BASE = 0x0100


@dataclass
class ProvisionedKeys:
    """A freshly provisioned key set: public entries + their private PEMs, and the roots
    that certified them."""

    trusted: list[TrustedKey]       # public set -> keys/trusted_keys.json (each with its cert)
    private_pems: dict[int, bytes]  # key_id -> PKCS#8 PEM (secured, gitignored)
    signing_key_id: int             # the current OTA signer (first ota key)
    roots: list[RootKey] = None     # public roots -> keys/root_keys.json
    root_pems: dict[int, bytes] = None  # root_id -> PKCS#8 PEM (keep OFFLINE)


def provision_roots(passphrase: str) -> tuple[list[RootKey], dict[int, bytes]]:
    """Mint the :data:`ROOT_COUNT` roots (P-256): the public set and the encrypted PEMs."""
    from .algorithms import ES256

    spec = algorithm_for(ES256)
    roots, pems = [], {}
    for root_id in range(ROOT_COUNT):
        priv = generate_private_key(spec)
        roots.append(RootKey(root_id, ES256, public_point_hex(priv.public_key())))
        pems[root_id] = private_key_pem(priv, passphrase)
    return roots, pems


def _certify_with(root_pems: dict[int, bytes], passphrase: str, keys: list[TrustedKey]) -> None:
    certify(load_private_key_pem(root_pems[0], passphrase), 0, keys)


def provision_key_set(alg: AlgSpec, n_factory: int, n_ota: int, passphrase: str) -> ProvisionedKeys:
    """Generate the whole key set for a new OTA project: two roots, then ``n_factory`` factory
    keys + an ``n_ota`` OTA rotation pool, each certified by root 0. Private PEMs are encrypted
    under ``passphrase``. The device embeds the roots; the current signer is the first OTA key.
    Certified keys are P-256, so ``alg`` must be ES256."""
    from .algorithms import ES256

    if alg.cose_id != ES256:
        raise OtaError("certificates carry P-256 keys only: the signing algorithm must be ES256")
    trusted: list[TrustedKey] = []
    private_pems: dict[int, bytes] = {}

    def _mint(key_id: int, role: str) -> None:
        priv = generate_private_key(alg)
        trusted.append(
            TrustedKey(key_id, alg.cose_id, role, public_point_hex(priv.public_key()))
        )
        private_pems[key_id] = private_key_pem(priv, passphrase)

    for i in range(n_factory):
        _mint(FACTORY_KEY_ID_BASE + i, "factory")
    for i in range(n_ota):
        _mint(OTA_KEY_ID_BASE + i, "ota")

    roots, root_pems = provision_roots(passphrase)
    _certify_with(root_pems, passphrase, trusted)
    return ProvisionedKeys(trusted, private_pems, OTA_KEY_ID_BASE, roots, root_pems)

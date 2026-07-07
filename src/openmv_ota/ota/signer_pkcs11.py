"""PKCS#11 signer backend -- one backend covering every hardware token / HSM that speaks PKCS#11
(YubiKey, Nitrokey, SoftHSM, Luna, CloudHSM, ...). The private key never leaves the token.

The signing logic works against a small ``session`` seam (``private_key(label)`` +
``public_point(label)``); ``build_signer`` opens a real ``python-pkcs11`` session, and tests inject
a fake one. The real-token adapter is excluded from host coverage (no hardware in CI).
"""

from __future__ import annotations

import hashlib

from .algorithms import AlgSpec
from .errors import OtaError
from .signer import Signer

_DIGEST = {"sha256": hashlib.sha256, "sha384": hashlib.sha384, "sha512": hashlib.sha512}


class Pkcs11Signer(Signer):
    """Signs a region by hashing it and asking the token to ECDSA-sign the digest (CKM_ECDSA
    returns raw ``R||S`` already). ``point`` is the token's uncompressed public EC point."""

    def __init__(self, key_id: int, sig_alg: int, alg: AlgSpec, *, priv, point: bytes):
        self.key_id = key_id
        self.sig_alg = sig_alg
        self.alg = alg
        self._priv = priv
        self._point = bytes(point)

    def sign(self, region: bytes) -> bytes:
        sig = bytes(self._priv.sign(_DIGEST[self.alg.hash_name](region).digest()))
        if len(sig) != self.alg.sig_size:
            raise OtaError("PKCS#11 returned a %d-byte signature, expected %d (raw R||S)"
                           % (len(sig), self.alg.sig_size))
        return sig

    def public_point_hex(self) -> str:
        return self._point.hex()


def build(entry, alg: AlgSpec, backend: dict, *, session=None) -> Pkcs11Signer:
    """Resolve a ``Pkcs11Signer`` for ``entry``. ``session`` is injected by tests; otherwise a real
    ``python-pkcs11`` session is opened from ``backend`` (module/token/object labels; PIN from the
    machine-local config)."""
    if session is None:  # pragma: no cover  (needs a real PKCS#11 token)
        from ._extras import require_extra
        require_extra("hsm")
        session = _open_session(backend)
    label = backend.get("object_label") or ("%s-%04x" % (entry.role, entry.key_id))
    return Pkcs11Signer(entry.key_id, entry.alg, alg,
                        priv=session.private_key(label), point=session.public_point(label))


def provisioner(backend: dict):  # pragma: no cover  (needs a real PKCS#11 token)
    """A ``KeyProvisioner`` that generates each keypair on the token via ``C_GenerateKeyPair`` and
    returns its public point + the per-key ``backends.json`` record."""
    from ._extras import require_extra
    from .signer import KeyProvisioner
    require_extra("hsm")
    import pkcs11
    from pkcs11.util.ec import encode_named_curve_parameters

    lib = pkcs11.lib(backend["pkcs11_module"])
    token = lib.get_token(token_label=backend.get("token_label"))
    session = token.open(user_pin=backend.get("pin"), rw=True)
    curve = {"sha256": "secp256r1", "sha384": "secp384r1", "sha512": "secp521r1"}

    class _Pkcs11Provisioner(KeyProvisioner):
        def provision(self, key_id, role, alg):
            label = "%s-%04x" % (role, key_id)
            params = session.create_domain_parameters(
                pkcs11.KeyType.EC,
                {pkcs11.Attribute.EC_PARAMS: encode_named_curve_parameters(curve[alg.hash_name])},
                local=True)
            pub, _priv = params.generate_keypair(store=True, label=label)
            point = _der_octet_string(bytes(pub[pkcs11.Attribute.EC_POINT]))
            record = {"backend": "pkcs11", "object_label": label,
                      "pkcs11_module": backend["pkcs11_module"]}
            if backend.get("token_label"):
                record["token_label"] = backend["token_label"]
            return point.hex(), record

    return _Pkcs11Provisioner()


# --- real PKCS#11 session (host-coverage excluded -- no token in CI) --------------------------

def _open_session(backend):  # pragma: no cover
    import pkcs11
    lib = pkcs11.lib(backend["pkcs11_module"])
    token = lib.get_token(token_label=backend.get("token_label"))
    return _RealSession(token.open(user_pin=backend.get("pin")), pkcs11)


class _RealSession:  # pragma: no cover
    def __init__(self, session, pkcs11):
        self._s = session
        self._p = pkcs11

    def private_key(self, label):
        return self._s.get_key(object_class=self._p.ObjectClass.PRIVATE_KEY, label=label)

    def public_point(self, label):
        pub = self._s.get_key(object_class=self._p.ObjectClass.PUBLIC_KEY, label=label)
        return _der_octet_string(bytes(pub[self._p.Attribute.EC_POINT]))


def _der_octet_string(data: bytes) -> bytes:  # pragma: no cover
    # PKCS#11 wraps the uncompressed EC point in a DER OCTET STRING (0x04 tag || length || point).
    n = data[1]
    if n < 0x80:
        return data[2:2 + n]
    k = n & 0x7F
    length = int.from_bytes(data[2:2 + k], "big")
    return data[2 + k:2 + k + length]

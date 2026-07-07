"""Cloud KMS signer backends (AWS KMS / GCP KMS / Azure Key Vault).

The private key lives in the provider's KMS and never leaves it -- the tool signs *through* the
Sign API. Each provider adapter normalizes its wire form to raw ``R||S`` + an uncompressed public
point, behind a small ``client`` seam so ``KmsSigner`` is provider-agnostic and fully testable with
a fake client. The real cloud adapters are excluded from host coverage (no cloud creds in CI) and
need real-cloud acceptance testing.
"""

from __future__ import annotations

import hashlib

from .algorithms import AlgSpec
from .errors import OtaError
from .signer import Signer

_HASH = {"sha256": hashlib.sha256, "sha384": hashlib.sha384, "sha512": hashlib.sha512}


class KmsSigner(Signer):
    """Delegates signing + the public point to a provider ``client`` (``sign(region, alg) -> raw
    R||S``, ``public_point_hex() -> str``)."""

    def __init__(self, key_id: int, sig_alg: int, alg: AlgSpec, *, client):
        self.key_id = key_id
        self.sig_alg = sig_alg
        self.alg = alg
        self._c = client

    def sign(self, region: bytes) -> bytes:
        sig = self._c.sign(region, self.alg)
        if len(sig) != self.alg.sig_size:
            raise OtaError("KMS returned a %d-byte signature, expected %d (raw R||S)"
                           % (len(sig), self.alg.sig_size))
        return sig

    def public_point_hex(self) -> str:
        return self._c.public_point_hex()


def build(entry, alg: AlgSpec, backend: dict, *, client=None) -> KmsSigner:
    """Resolve a ``KmsSigner``. ``client`` is injected by tests; otherwise the provider client is
    built from ``backend`` (``uri`` = the key's ARN / resource name / vault key URL)."""
    if client is None:  # pragma: no cover  (needs cloud credentials)
        from ._extras import require_extra
        tag = backend["backend"]
        require_extra(tag)
        client = _CLIENTS[tag](backend)
    return KmsSigner(entry.key_id, entry.alg, alg, client=client)


# --- real provider adapters (host-coverage excluded -- no cloud creds in CI) ------------------

def _aws(backend):  # pragma: no cover
    import boto3
    from .sign import der_to_raw

    kms = boto3.client("kms", region_name=backend.get("region"))
    key = backend["uri"]
    alg_name = {"sha256": "ECDSA_SHA_256", "sha384": "ECDSA_SHA_384", "sha512": "ECDSA_SHA_512"}

    class _Aws:
        def sign(self, region, alg):
            der = kms.sign(KeyId=key, Message=region, MessageType="RAW",
                           SigningAlgorithm=alg_name[alg.hash_name])["Signature"]
            return der_to_raw(der, alg)

        def public_point_hex(self):
            from .keys import spki_to_point_hex
            return spki_to_point_hex(kms.get_public_key(KeyId=key)["PublicKey"])

    return _Aws()


def _gcp(backend):  # pragma: no cover
    from google.cloud import kms as gkms

    from .keys import spki_to_point_hex
    from .sign import der_to_raw
    client = gkms.KeyManagementServiceClient()
    name = backend["uri"]   # the crypto key *version* resource name

    class _Gcp:
        def sign(self, region, alg):
            h = _HASH[alg.hash_name](region).digest()
            resp = client.asymmetric_sign(request={"name": name, "digest": {alg.hash_name: h}})
            return der_to_raw(resp.signature, alg)

        def public_point_hex(self):
            from cryptography.hazmat.primitives import serialization
            pem = client.get_public_key(request={"name": name}).pem.encode()
            der = serialization.load_pem_public_key(pem).public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
            return spki_to_point_hex(der)

    return _Gcp()


def _azure(backend):  # pragma: no cover
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.keys.crypto import CryptographyClient, SignatureAlgorithm

    from .keys import public_point_hex
    cc = CryptographyClient(backend["uri"], DefaultAzureCredential())
    sig_alg = {"sha256": SignatureAlgorithm.es256, "sha384": SignatureAlgorithm.es384,
               "sha512": SignatureAlgorithm.es512}

    class _Azure:
        def sign(self, region, alg):
            h = _HASH[alg.hash_name](region).digest()
            return cc.sign(sig_alg[alg.hash_name], h).signature   # Azure returns raw R||S already

        def public_point_hex(self):
            from cryptography.hazmat.primitives.asymmetric import ec
            jwk = cc.key.key
            pub = ec.EllipticCurvePublicNumbers(
                int.from_bytes(jwk.x, "big"), int.from_bytes(jwk.y, "big"),
                ec.SECP256R1()).public_key()
            return public_point_hex(pub)

    return _Azure()


_CLIENTS = {"aws-kms": _aws, "gcp-kms": _gcp, "azure-kms": _azure}

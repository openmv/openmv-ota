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


def provisioner(backend: dict):  # pragma: no cover  (needs cloud credentials)
    """A ``KeyProvisioner`` that creates a fresh signing key in the provider and returns its public
    point + the per-key ``backends.json`` record. NOTE: each key is billable -- mint a small pool."""
    from ._extras import require_extra
    from .signer import KeyProvisioner
    tag = backend["backend"]
    require_extra(tag)
    make = _PROVISIONERS[tag]

    class _KmsProvisioner(KeyProvisioner):
        def provision(self, key_id, role, alg):
            return make(backend, key_id, role, alg)

    return _KmsProvisioner()


def _aws_provision(backend, key_id, role, alg):  # pragma: no cover
    import boto3

    from .keys import spki_to_point_hex
    kms = boto3.client("kms", region_name=backend.get("region"))
    spec = {"sha256": "ECC_NIST_P256", "sha384": "ECC_NIST_P384", "sha512": "ECC_NIST_P521"}
    meta = kms.create_key(KeyUsage="SIGN_VERIFY", KeySpec=spec[alg.hash_name],
                          Description="openmv-ota %s-%04x" % (role, key_id))["KeyMetadata"]
    arn = meta["Arn"]
    point = spki_to_point_hex(kms.get_public_key(KeyId=arn)["PublicKey"])
    record = {"backend": "aws-kms", "uri": arn}
    if backend.get("region"):
        record["region"] = backend["region"]
    return point, record


def _gcp_provision(backend, key_id, role, alg):  # pragma: no cover
    from cryptography.hazmat.primitives import serialization
    from google.cloud import kms as gkms

    from .keys import spki_to_point_hex
    client = gkms.KeyManagementServiceClient()
    parent = backend["key_ring"]   # projects/*/locations/*/keyRings/*
    algo = {"sha256": "EC_SIGN_P256_SHA256", "sha384": "EC_SIGN_P384_SHA384",
            "sha512": "EC_SIGN_P521_SHA512"}
    ck = client.create_crypto_key(request={
        "parent": parent, "crypto_key_id": "openmv-ota-%s-%04x" % (role, key_id),
        "crypto_key": {"purpose": gkms.CryptoKey.CryptoKeyPurpose.ASYMMETRIC_SIGN,
                       "version_template": {"algorithm": algo[alg.hash_name]}}})
    version = client.list_crypto_key_versions(request={"parent": ck.name}).crypto_key_versions[0].name
    pem = client.get_public_key(request={"name": version}).pem.encode()
    der = serialization.load_pem_public_key(pem).public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return spki_to_point_hex(der), {"backend": "gcp-kms", "uri": version}


def _azure_provision(backend, key_id, role, alg):  # pragma: no cover
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.keys import KeyClient

    from .keys import public_point_hex
    from cryptography.hazmat.primitives.asymmetric import ec
    kc = KeyClient(backend["vault_url"], DefaultAzureCredential())
    curve = {"sha256": "P-256", "sha384": "P-384", "sha512": "P-521"}
    key = kc.create_ec_key("openmv-ota-%s-%04x" % (role, key_id), curve=curve[alg.hash_name])
    jwk = key.key
    pub = ec.EllipticCurvePublicNumbers(
        int.from_bytes(jwk.x, "big"), int.from_bytes(jwk.y, "big"),
        {"P-256": ec.SECP256R1, "P-384": ec.SECP384R1, "P-521": ec.SECP521R1}[curve[alg.hash_name]]()
    ).public_key()
    return public_point_hex(pub), {"backend": "azure-kms", "uri": key.id}


_PROVISIONERS = {"aws-kms": _aws_provision, "gcp-kms": _gcp_provision, "azure-kms": _azure_provision}


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

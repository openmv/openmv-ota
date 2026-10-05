"""The Azure Key Vault signer against the REAL Key Vault -- the live pass the fakes cannot give.

Opt-in (like the SoftHSM, GCP and AWS tests): set ``OPENMV_OTA_AZURE_KEY_ID`` to a P-256 EC
key's id (its versioned URL) and have credentials azure-identity can find (``az login``, or
CI's federated credential). Uses an existing key, so re-running creates nothing and bills
nothing new. First passed 2026-10-04, alongside a full `project keys backend provision` +
`build romfs` whose image `verify_image` accepted -- the pass that found the adapter reading
the public key from an attribute the real crypto client does not have.
"""
import os
from types import SimpleNamespace

import pytest

KEY_ID = os.environ.get("OPENMV_OTA_AZURE_KEY_ID", "")
pytestmark = pytest.mark.skipif(not KEY_ID, reason="set OPENMV_OTA_AZURE_KEY_ID to run live")


def test_signs_through_azure_key_vault_and_the_device_check_accepts_it():
    pytest.importorskip("azure.keyvault.keys")
    from openmv_ota.ota import signer_kms
    from openmv_ota.ota.algorithms import algorithm_for
    from openmv_ota.ota.keys import public_key_from_hex
    from openmv_ota.ota.sign import verify_region

    alg = algorithm_for(-7)                                   # ES256: P-256 / SHA-256
    signer = signer_kms.build(SimpleNamespace(key_id=0x0100, alg=-7), alg,
                              {"backend": "azure-kms", "uri": KEY_ID})
    point = signer.public_point_hex()
    assert point.startswith("04") and len(point) == 130      # uncompressed P-256 point
    pub = public_key_from_hex(point, alg)
    for msg in (b"openmv-ota live pass", os.urandom(96)):
        sig = signer.sign(msg)
        assert len(sig) == alg.sig_size                      # raw R||S, not DER
        assert verify_region(pub, msg, sig, alg)
        assert not verify_region(pub, msg + b"x", sig, alg)

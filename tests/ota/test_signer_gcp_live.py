"""The GCP KMS signer against the REAL Cloud KMS service -- the live pass the fakes cannot give.

Opt-in (like the SoftHSM test): set ``OPENMV_OTA_GCP_KEY_VERSION`` to an EC P-256 signing key
version (``projects/*/locations/*/keyRings/*/cryptoKeys/*/cryptoKeyVersions/*``) and have
credentials the Google client library can find (``gcloud auth application-default login``).
Uses an existing key, so re-running creates nothing and bills nothing new. First passed
2026-09-27 against <key-ring>, alongside a full
`project keys backend provision` + `build romfs` whose image `verify_image` accepted.
"""
import os
from types import SimpleNamespace

import pytest

VERSION = os.environ.get("OPENMV_OTA_GCP_KEY_VERSION", "")
pytestmark = pytest.mark.skipif(not VERSION, reason="set OPENMV_OTA_GCP_KEY_VERSION to run live")


def test_signs_through_cloud_kms_and_the_device_check_accepts_it():
    pytest.importorskip("google.cloud.kms")
    from openmv_ota.ota import signer_kms
    from openmv_ota.ota.algorithms import algorithm_for
    from openmv_ota.ota.keys import public_key_from_hex
    from openmv_ota.ota.sign import verify_region

    alg = algorithm_for(-7)                                   # ES256: P-256 / SHA-256
    signer = signer_kms.build(SimpleNamespace(key_id=0x0100, alg=-7), alg,
                              {"backend": "gcp-kms", "uri": VERSION})
    point = signer.public_point_hex()
    assert point.startswith("04") and len(point) == 130      # uncompressed P-256 point
    pub = public_key_from_hex(point, alg)
    for region in (b"openmv-ota live pass", os.urandom(96)):
        sig = signer.sign(region)
        assert len(sig) == alg.sig_size                      # raw R||S, not DER
        assert verify_region(pub, region, sig, alg)
        assert not verify_region(pub, region + b"x", sig, alg)

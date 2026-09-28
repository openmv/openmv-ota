"""The AWS KMS signer against the REAL KMS service -- the live pass the fakes cannot give.

Opt-in (like the SoftHSM and GCP tests): set ``OPENMV_OTA_AWS_KEY_ARN`` to an ECC_NIST_P256
SIGN_VERIFY key's ARN and have credentials boto3 can find (``aws login``, or CI's OIDC role).
Uses an existing key, so re-running creates nothing and bills nothing new. First passed
2026-09-28, alongside a full `project keys backend provision` + `build romfs` whose image
`verify_image` accepted.
"""
import os
from types import SimpleNamespace

import pytest

ARN = os.environ.get("OPENMV_OTA_AWS_KEY_ARN", "")
pytestmark = pytest.mark.skipif(not ARN, reason="set OPENMV_OTA_AWS_KEY_ARN to run live")


def test_signs_through_aws_kms_and_the_device_check_accepts_it():
    pytest.importorskip("boto3")
    from openmv_ota.ota import signer_kms
    from openmv_ota.ota.algorithms import algorithm_for
    from openmv_ota.ota.keys import public_key_from_hex
    from openmv_ota.ota.sign import verify_region

    alg = algorithm_for(-7)                                   # ES256: P-256 / SHA-256
    region = ARN.split(":")[3]                                # arn:aws:kms:<region>:...
    signer = signer_kms.build(SimpleNamespace(key_id=0x0100, alg=-7), alg,
                              {"backend": "aws-kms", "uri": ARN, "region": region})
    point = signer.public_point_hex()
    assert point.startswith("04") and len(point) == 130      # uncompressed P-256 point
    pub = public_key_from_hex(point, alg)
    for msg in (b"openmv-ota live pass", os.urandom(96)):
        sig = signer.sign(msg)
        assert len(sig) == alg.sig_size                      # raw R||S, not DER
        assert verify_region(pub, msg, sig, alg)
        assert not verify_region(pub, msg + b"x", sig, alg)

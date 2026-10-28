"""The roots the host tool and the server verify servers against.

Eight eMudhra (emSign) intermediate CA keys were compromised in October 2026; certifi
2026.10.19 (what httpx reads) and botocore 1.43.117 (its own bundle, the S3/KMS extras)
dropped the root that signed them. pyproject floors both, and this guards the environment
that actually resolved: a cached server-image build or a stale lock that kept an older
bundle would still trust that root, with every floor in place.
"""
from pathlib import Path

import certifi
import pytest

_ROOT = "emSign ECC Root CA - G3"


def test_the_resolved_certifi_has_no_emsign_g3_root():
    assert tuple(int(x) for x in certifi.__version__.split(".")) >= (2026, 10, 19)
    assert _ROOT not in Path(certifi.where()).read_text(encoding="utf-8")


def test_the_resolved_botocore_bundle_has_no_emsign_g3_root():
    botocore = pytest.importorskip("botocore")     # the server-s3 / aws-kms extras
    assert tuple(int(x) for x in botocore.__version__.split(".")) >= (1, 43, 117)
    assert _ROOT not in Path(botocore.__file__).with_name("cacert.pem").read_text(encoding="utf-8")

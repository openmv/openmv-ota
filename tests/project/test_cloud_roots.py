"""The hosted OpenMV Cloud's trust anchors (src/openmv_ota/data/openmv-cloud-roots.pem).

`project new` scaffolds this file as certs/root.pem for boards whose firmware cannot carry the
public bundle, and `build firmware` freezes it as the device's TLS trust store. It is pinned
here so it only changes on purpose: every certificate is a known, self-signed root (by subject
AND fingerprint), and the file stays a few KB per root -- it lives in FLASH_TEXT on the
1792 KB boards."""
import datetime
import re

from cryptography import x509
from cryptography.hazmat.primitives import hashes

from openmv_ota.project.project import CLOUD_ROOTS

# The roots of every CA Cloudflare may issue the cloud hosts' edge certificates from: Google
# Trust Services (today's chain: WE1 -> GTS Root R4, WR1 -> GTS Root R1), Let's Encrypt and
# SSL.com (Universal SSL), and Sectigo (backup certificates).
PINNED = [
    ("CN=GTS Root R1,O=Google Trust Services LLC,C=US",
     "d947432abde7b7fa90fc2e6b59101b1280e0e1c7e4e40fa3c6887fff57a7f4cf"),
    ("CN=GTS Root R3,O=Google Trust Services LLC,C=US",
     "34d8a73ee208d9bcdb0d956520934b4e40e69482596e8b6f73c8426b010a6f48"),
    ("CN=GTS Root R4,O=Google Trust Services LLC,C=US",
     "349dfa4058c5e263123b398ae795573c4e1313c83fe68f93556cd5e8031b3c7d"),
    ("CN=ISRG Root X1,O=Internet Security Research Group,C=US",
     "96bcec06264976f37460779acf28c5a7cfe8a3c0aae11a8ffcee05c0bddf08c6"),
    ("CN=ISRG Root X2,O=Internet Security Research Group,C=US",
     "69729b8e15a86efc177a57afb7171dfc64add28c2fca8cf1507e34453ccb1470"),
    ("CN=SSL.com Root Certification Authority RSA,O=SSL Corporation,L=Houston,ST=Texas,C=US",
     "85666a562ee0be5ce925c1d8890a6f76a87ec16d4d7d5f29ea7419cf20123b69"),
    ("CN=SSL.com Root Certification Authority ECC,O=SSL Corporation,L=Houston,ST=Texas,C=US",
     "3417bb06cc6007da1b961c920b8ab4ce3fad820e4aa30b9acbc4a74ebdcebc65"),
    ("CN=SSL.com TLS RSA Root CA 2022,O=SSL Corporation,C=US",
     "8faf7d2e2cb4709bb8e0b33666bf75a5dd45b5de480f8ea8d4bfe6bebc17f2ed"),
    ("CN=SSL.com TLS ECC Root CA 2022,O=SSL Corporation,C=US",
     "c32ffd9f46f936d16c3673990959434b9ad60aafbb9e7cf33654f144cc1ba143"),
    ("CN=Sectigo Public Server Authentication Root R46,O=Sectigo Limited,C=GB",
     "7bb647a62aeeac88bf257aa522d01ffea395e0ab45c73f93f65654ec38f25a06"),
    ("CN=Sectigo Public Server Authentication Root E46,O=Sectigo Limited,C=GB",
     "c90f26f0fb1b4018b22227519b5ca2b53e2ca5b3be5cf18efe1bef47380c5383"),
]

# A 4096-bit RSA root is ~1.9-2.1 KB of PEM, an EC root under 1 KB. Past this the file is
# carrying something other than a handful of roots.
MAX_BYTES_PER_ROOT = 2300


def _certs():
    text = CLOUD_ROOTS.read_text(encoding="ascii")
    blocks = re.findall(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", text, re.S)
    return text, [x509.load_pem_x509_certificate(b.encode()) for b in blocks]


def test_the_cloud_roots_are_exactly_the_pinned_set():
    _text, certs = _certs()
    got = [(c.subject.rfc4514_string(), c.fingerprint(hashes.SHA256()).hex()) for c in certs]
    assert got == PINNED


def test_every_cloud_root_is_a_self_signed_ca_still_in_date():
    """Anchors, not intermediates: a server's chain can switch intermediates under them."""
    _text, certs = _certs()
    now = datetime.datetime.now(datetime.timezone.utc)
    for c in certs:
        assert c.subject == c.issuer, c.subject
        assert c.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
        assert c.not_valid_before_utc < now < c.not_valid_after_utc, c.subject


def test_the_cloud_roots_stay_a_few_kb_per_root():
    """Frozen into FLASH_TEXT on boards with little to spare: it must not quietly grow."""
    text, certs = _certs()
    assert len(text.encode()) <= MAX_BYTES_PER_ROOT * len(certs)


def test_the_cloud_roots_say_how_to_self_host():
    text, _certs_ = _certs()
    assert text.startswith("# Anchors for the hosted OpenMV Cloud; self-hosting? Replace with "
                           "your server's root.\n")

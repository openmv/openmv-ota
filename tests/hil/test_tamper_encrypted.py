"""The corrupt_sha scenario, exercised on the host.

That scenario asks: does a device reject an image whose bytes decrypt and decompress
cleanly but no longer match the digest its signed manifest carries? Producing such an
image now means taking it apart with the project's payload key and putting it back
together under the SAME key and iv -- real work, in the harness, that ran for the first
time on a bench and died on `unsupported operand type(s) for /: 'str' and 'str'` four
minutes into a leg. It is all host-testable, so here it is.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import random
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..", "..", "ci", "hil")))
os.environ.setdefault("WIFI_SSID", "")
os.environ.setdefault("WIFI_PASSWORD", "")

import ota_cycle  # noqa: E402

from openmv_ota.ota import payload  # noqa: E402
from openmv_ota.ota.algorithms import ES256  # noqa: E402
from openmv_ota.ota.manifest import Manifest, pack_manifest  # noqa: E402
from openmv_ota.project import payload_keys as pk  # noqa: E402

BOARD = "OPENMV4P"
PASS = "bench-passphrase"


def _release(tmp_path):
    """A published release as the store holds it: encrypted artifact + signed manifest,
    with the project's payload keys where the harness will look for them."""
    project = tmp_path / "proj"
    (project / "keys").mkdir(parents=True)
    (project / "keys" / ".dev-passphrase").write_text(PASS)      # what resolve_passphrase finds
    keys = {1: payload.new_key()}
    pk.write(project / "keys" / "private", {BOARD: keys}, PASS)

    # SLOT-SHAPED, like the real thing: a small varied body followed by erased flash. That
    # shape is the whole difficulty -- it gzips ~50:1, so anything that makes it less
    # compressible will not fit back inside the length the signed manifest declares.
    rnd = random.Random(7)
    body = bytes(rnd.randrange(256) for _ in range(40000))
    image = body + b"\xFF" * (512 * 1024 - len(body))
    gz = gzip.compress(image, mtime=0)
    ciphertext, enc = payload.encrypt_artifact(gz, keys)
    artifact = tmp_path / ("%s-ota.img.gz.enc" % BOARD)
    artifact.write_bytes(ciphertext)

    body = {"schema": 1, "product_id": 7, "product": "P", "version": "1.2.0",
            "payload_version": 0x01020000, "min_platform_version": 0,
            "size": len(image), "sha256": hashlib.sha256(image).hexdigest(),
            "representations": [{"format": "full", "url": artifact.name,
                                 "size": len(ciphertext), "enc": enc}]}
    manifest = tmp_path / "manifest.bin"
    manifest.write_bytes(pack_manifest(Manifest(body=body, key_id=0x0100, sig_alg=ES256,
                                                signature=b"\x00" * 64)))
    return project, artifact, manifest, keys, enc, image


def test_the_tampered_image_still_decrypts_but_no_longer_matches_its_digest(tmp_path,
                                                                            monkeypatch):
    project, artifact, manifest, keys, enc, image = _release(tmp_path)
    monkeypatch.setitem(ota_cycle.CFG, "project", str(project))   # a str, as the harness holds it

    before = artifact.read_bytes()
    ota_cycle._tamper_image_body(str(artifact), str(manifest), BOARD)
    after = artifact.read_bytes()
    assert after != before

    # the SIGNED manifest is untouched, so the artifact must still decrypt under it...
    plain = payload.decrypt_artifact(after, keys, enc)
    tampered = gzip.decompress(plain)                             # ...and still inflate
    # ...but be a different image, which is the gate the scenario exists to hit
    assert tampered != image
    assert len(tampered) == len(image)
    assert hashlib.sha256(tampered).hexdigest() != hashlib.sha256(image).hexdigest()


def test_the_artifact_keeps_the_length_the_signed_manifest_declares(tmp_path, monkeypatch):
    """A device reads exactly `enc.size` bytes. An artifact that grew or shrank would fail
    somewhere else entirely, and the scenario would pass for the wrong reason."""
    project, artifact, manifest, _keys, enc, _image = _release(tmp_path)
    monkeypatch.setitem(ota_cycle.CFG, "project", str(project))

    size_before = artifact.stat().st_size
    ota_cycle._tamper_image_body(str(artifact), str(manifest), BOARD)
    assert artifact.stat().st_size == size_before == enc["size"] + (-enc["size"] % 16)

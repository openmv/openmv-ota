"""The formats that cross between the build, the server and a camera, pinned to sample bytes.

Once cameras are deployed these cannot change shape: a release the server holds has to stay
installable, and a camera in the field has to keep being understood. ``format_pins.json``
holds one sample of each, made from the fixed inputs below. Every test here checks both
directions: today's code still PRODUCES the sample from those inputs, and still READS it.

A failure means a format changed. Extending one compatibly (a new optional manifest key, a
field taken out of the trailer's ``reserved``) adds a new sample beside the old one; the old
sample stays, and stays readable. Replacing a sample is a break with every camera that
shipped.
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from openmv_ota.ota import (
    boot_journal,
    certificate,
    delta,
    lockbox,
    manifest,
    payload,
    registration,
    status,
    trailer,
)

PINS = json.loads((Path(__file__).parent / "format_pins.json").read_text())

K1 = ec.derive_private_key(int("11" * 32, 16), ec.SECP256R1())
K2 = ec.derive_private_key(int("22" * 32, 16), ec.SECP256R1())
SIG = bytes(range(64))                      # the codecs carry a signature, they do not make one
KEY = bytes(range(32))
IV = bytes(range(16, 32))


def _point(key) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.X962,
                                         serialization.PublicFormat.UncompressedPoint)


def _certificate() -> bytes:
    return certificate.pack(certificate.Certificate(
        kind=certificate.KIND_OTA, root_id=0, epoch=3, pubkey=_point(K1), signature=SIG))


def test_the_certificate_is_the_bytes_it_was():
    assert _certificate().hex() == PINS["certificate"]
    c = certificate.parse(bytes.fromhex(PINS["certificate"]))
    assert (c.kind, c.root_id, c.epoch, c.pubkey, c.signature) == (
        certificate.KIND_OTA, 0, 3, _point(K1), SIG)
    assert certificate.SIZE == 160 and certificate.SIGNED_SIZE == 96 and certificate.VERSION == 1


def _trailer() -> trailer.Trailer:
    return trailer.Trailer(
        body_size=8192, meta={"app_version": "1.2.3", "board": "OPENMV_N6"},
        product_id=0x1122334455667788, payload_version=0x00010203, body_sha256=bytes(range(32)),
        certificate=_certificate(),
        components=[trailer.Component(dest=0x70100000, offset=8192, size=1000, installed_size=4000,
                                      sha256=b"\xaa" * 32, installed_sha256=b"\xbb" * 32)],
        min_stub=1, uart_en=1, uart_id=4, wdt_en=1, wdt_id=0, baud=115200, wdt_timeout_ms=100,
        publish_seq=7, reserved=0, signature=SIG)


def test_the_image_trailer_is_the_bytes_it_was():
    assert trailer.pack_trailer(_trailer()).hex() == PINS["trailer"]
    t = trailer.parse_trailer(bytes.fromhex(PINS["trailer"]))
    assert t == _trailer()
    assert (trailer.HEADER_VERSION, trailer.HEADER_SIZE, trailer.COMPONENT_SIZE,
            trailer.TRAILER_SZ, trailer.MAGIC_ROMFS_APP) == (4, 96, 80, 4096, b"OMVR")


def test_a_trailer_using_the_reserved_field_still_parses():
    """The one piece of headroom in the header: a later build may put a number there, and
    today's reader must carry it rather than refuse the image."""
    later = _trailer()
    later.reserved = 0x0102030405060708
    assert trailer.parse_trailer(trailer.pack_trailer(later)).reserved == 0x0102030405060708


def _manifest() -> manifest.Manifest:
    return manifest.Manifest(
        body={"schema": 1, "product_id": 5, "product": "p", "version": "1.2.3",
              "payload_version": 66051, "publish_seq": 0, "min_platform_version": 0, "min_stub": 0,
              "size": 4096, "sha256": "00" * 32,
              "representations": [{"format": "full", "url": "https://x.example/a", "size": 100}]},
        certificate=_certificate(), signature=SIG)


def test_the_update_manifest_is_the_bytes_it_was():
    assert manifest.pack_manifest(_manifest()).hex() == PINS["manifest"]
    m = manifest.parse_manifest(bytes.fromhex(PINS["manifest"]))
    assert (m.body, m.certificate, m.signature) == (_manifest().body, _certificate(), SIG)
    assert (manifest.HEADER_VERSION, manifest.SCHEMA, manifest.MAGIC, manifest.DELTA_FORMAT) == (
        2, 1, b"OMVM", "ocdl")


def test_a_manifest_with_a_key_nobody_knows_yet_is_still_accepted():
    body = dict(_manifest().body, something_new={"a": 1})
    m = manifest.parse_manifest(manifest.pack_manifest(
        manifest.Manifest(body=body, certificate=_certificate(), signature=SIG)))
    assert m.body["something_new"] == {"a": 1}
    assert manifest.update_reject_reason(m.body, 5, 0, 0) is None


def test_an_encrypted_artifact_is_the_bytes_it_was():
    iv, ciphertext = payload.encrypt(b"openmv payload pin", KEY, IV)
    assert (iv, ciphertext.hex()) == (IV, PINS["payload_ciphertext"])
    assert payload.decrypt(bytes.fromhex(PINS["payload_ciphertext"]), KEY, IV, 18) == b"openmv payload pin"
    assert (payload.ALG, payload.KEY_SIZE, payload.WRAP_SIZE) == ("aes-256-cbc", 32, 48)
    board_key = bytes(range(32, 64))
    assert payload.unwrap_key(board_key, payload.wrap_key(board_key, KEY)) == KEY


def test_a_cameras_lockbox_still_opens():
    """A lockbox is made with a fresh key every time, so the sample is only ever read."""
    box = bytes.fromhex(PINS["lockbox"])
    assert len(box) == lockbox.LOCKBOX_SIZE == 145
    assert lockbox.open_lockbox(box, K2) == KEY
    assert (lockbox.LOCKBOX_INFO, lockbox.SEAL_INFO) == (b"omv-lockbox-v1", b"omv-seal-v1")


def test_a_delta_patch_is_the_bytes_it_was():
    base = bytes(range(256)) * 4
    target = base[:300] + b"changed bytes here" + base[320:] + b"tail"
    assert delta.make_delta(base, target).hex() == PINS["delta"]
    assert delta.apply_delta(base, bytes.fromhex(PINS["delta"])) == target
    assert delta.MAGIC == b"OCDL"


def test_a_registration_record_is_the_bytes_it_was():
    record = registration.build(
        account="acct_1", device_id="0011223344556677", board="OPENMV_N6",
        identity=_point(K1).hex().upper(), exchange=_point(K2).hex(), desk=0x300,
        issued=datetime(2026, 11, 12, 1, 2, 3, tzinfo=timezone.utc))
    assert record.decode() == PINS["registration"]
    assert registration.VERSION == 1


def test_the_slot_status_sector_is_the_bytes_it_was():
    """Written by the installer a release carries, read by a stub that only USB replaces."""
    usual = status.build_status_sector(4096, pending=False, tried=False, confirmed=True,
                                       counter=2, floor_key=66051, epoch=3)
    wide = status.build_status_sector(4096, pending=True, tried=False, confirmed=False,
                                      counter=5, stride=32, floor_key=66051, epoch=3)
    assert hashlib.sha256(usual).hexdigest() == PINS["status_sector_sha256"]
    assert hashlib.sha256(wide).hexdigest() == PINS["status_sector_stride32_sha256"]
    assert {"pending": status.PENDING.hex(), "tried": status.TRIED.hex(),
            "confirmed": status.CONFIRMED.hex(), "repr_full": status.REPR_FULL.hex(),
            "repr_delta": status.REPR_DELTA.hex(), "attempt": status.ATTEMPT.hex()} == PINS["status_markers"]


def test_a_boot_journal_entry_is_the_bytes_it_was_and_the_wire_takes_longer_ones():
    entry = boot_journal.Entry(slot=1, reason=4, cause_a=26, cause_b=0, seq=3, events=0x104)
    assert boot_journal.pack(entry).hex() == PINS["journal_entry"]
    block = bytes.fromhex(PINS["journal_entry"]) + b"\xff" * (boot_journal.BLOCK - boot_journal.OFFSET - 16)
    assert boot_journal.parse(b"\xff" * boot_journal.OFFSET + block) == [entry]
    # the check-in's form: six numbers; what a later stub appends is ignored, not fatal
    assert boot_journal.from_wire([[3, 1, 4, 26, 0, 0x104]]) == [entry]
    assert boot_journal.from_wire([[3, 1, 4, 26, 0, 0x104, 99, 7]]) == [entry]
    assert boot_journal.from_wire([[3, 1, 4, 26, 0]]) == []

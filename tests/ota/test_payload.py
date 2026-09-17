"""Tests for payload encryption -- the artifact a camera downloads is ciphertext."""

from __future__ import annotations

import hashlib

import pytest

from openmv_ota.ota import payload
from openmv_ota.ota.errors import OtaError

IMAGE = b"\x1f\x8b" + b"a gzipped slot image, more or less" * 97   # not block-aligned


def test_a_fresh_key_is_256_bits_and_never_the_same_twice():
    assert len(payload.new_key()) == payload.KEY_SIZE
    assert payload.new_key() != payload.new_key()


def test_the_artifact_round_trips_through_the_manifest_block():
    keys = {1: payload.new_key()}
    ciphertext, enc = payload.encrypt_artifact(IMAGE, keys)
    assert payload.decrypt_artifact(ciphertext, keys, enc) == IMAGE
    assert list(enc["wraps"]) == ["1"] and enc["alg"] == payload.ALG
    assert enc["size"] == len(IMAGE)
    # the digest in the block is the CIPHERTEXT's: it is the only one the update
    # server -- which must not be able to read the image -- can check for itself
    assert enc["sha256"] == hashlib.sha256(ciphertext).hexdigest()
    assert enc["sha256"] != hashlib.sha256(IMAGE).hexdigest()


def test_the_bytes_that_leave_the_build_are_not_the_image():
    ciphertext, _ = payload.encrypt_artifact(IMAGE, {1: payload.new_key()})
    assert IMAGE[:64] not in ciphertext
    assert not ciphertext.startswith(b"\x1f\x8b")           # not even the gzip magic
    assert len(ciphertext) % payload.BLOCK == 0             # whole blocks, always


def test_two_releases_never_share_a_keystream():
    """The board key only unwraps; the bulk key is fresh per artifact, so the same
    image published twice produces unrelated bytes."""
    board = payload.new_key()
    first, enc_a = payload.encrypt_artifact(IMAGE, {1: board})
    second, enc_b = payload.encrypt_artifact(IMAGE, {1: board})
    assert first != second
    assert enc_a["wraps"] != enc_b["wraps"]
    assert payload.unwrap_key(board, bytes.fromhex(enc_a["wraps"]["1"])) \
        != payload.unwrap_key(board, bytes.fromhex(enc_b["wraps"]["1"]))


def test_a_wrap_is_the_key_under_the_board_key():
    board, content = payload.new_key(), payload.new_key()
    wrap = payload.wrap_key(board, content)
    assert len(wrap) == payload.WRAP_SIZE
    assert content not in wrap                              # not just sitting there
    assert payload.unwrap_key(board, wrap) == content
    assert payload.unwrap_key(payload.new_key(), wrap) != content   # wrong board, wrong key


def test_the_wrong_board_key_does_not_recover_the_image():
    ciphertext, enc = payload.encrypt_artifact(IMAGE, {1: payload.new_key()})
    assert payload.decrypt_artifact(ciphertext, {1: payload.new_key()}, enc) != IMAGE


def test_a_given_iv_is_used_as_given():
    """Re-encrypting bytes a SIGNED manifest already describes -- what the HIL rig does to
    build a tampered image that still decrypts. A new artifact never takes this path."""
    key = payload.new_key()
    iv, first = payload.encrypt(IMAGE, key)
    again_iv, again = payload.encrypt(IMAGE, key, iv)
    assert again_iv == iv and again == first
    assert payload.encrypt(IMAGE, key)[1] != first          # ...and without one, fresh each time


def test_an_empty_artifact_still_encrypts():
    keys = {1: payload.new_key()}
    ciphertext, enc = payload.encrypt_artifact(b"", keys)
    assert enc["size"] == 0 and ciphertext == b""
    assert payload.decrypt_artifact(ciphertext, keys, enc) == b""


def test_a_block_aligned_artifact_is_not_padded_to_a_whole_extra_block():
    """Zero padding, not PKCS#7: an artifact that already fits gains nothing, and
    the device stops at the declared size rather than at a padding byte."""
    keys = {1: payload.new_key()}
    aligned = b"x" * (payload.BLOCK * 4)
    ciphertext, enc = payload.encrypt_artifact(aligned, keys)
    assert len(ciphertext) == len(aligned)
    assert payload.decrypt_artifact(ciphertext, keys, enc) == aligned


@pytest.mark.parametrize("length", [5, 16, 33])
def test_a_key_of_the_wrong_length_is_refused(length):
    with pytest.raises(OtaError, match="payload key must be 32 bytes"):
        payload.encrypt(b"data", b"k" * length)


def test_an_iv_of_the_wrong_length_is_refused():
    with pytest.raises(OtaError, match="payload iv must be 16 bytes"):
        payload.decrypt(b"\0" * 16, payload.new_key(), b"\0" * 8, 16)


def test_a_content_key_of_the_wrong_length_is_refused():
    with pytest.raises(OtaError, match="content key must be 32 bytes"):
        payload.wrap_key(payload.new_key(), b"\0" * 16)


def test_a_truncated_wrap_is_refused_rather_than_guessed_at():
    with pytest.raises(OtaError, match="wrap must be 48 bytes"):
        payload.unwrap_key(payload.new_key(), b"\0" * 32)


def test_a_truncated_ciphertext_fails_loudly():
    keys = {1: payload.new_key()}
    ciphertext, enc = payload.encrypt_artifact(IMAGE, keys)
    with pytest.raises(OtaError, match="not a whole number of blocks"):
        payload.decrypt_artifact(ciphertext[:-1], keys, enc)


def test_a_size_that_reaches_past_the_ciphertext_is_refused():
    """The size comes out of the manifest. It is signed, but it is still a length
    field off the wire, and a length field is checked before it is used."""
    keys = {1: payload.new_key()}
    ciphertext, enc = payload.encrypt_artifact(IMAGE, keys)
    enc["size"] = len(ciphertext) + 1
    with pytest.raises(OtaError, match="is not inside"):
        payload.decrypt_artifact(ciphertext, keys, enc)
    enc["size"] = -1
    with pytest.raises(OtaError, match="is not inside"):
        payload.decrypt_artifact(ciphertext, keys, enc)


def test_an_unknown_algorithm_is_refused_rather_than_assumed():
    keys = {1: payload.new_key()}
    ciphertext, enc = payload.encrypt_artifact(IMAGE, keys)
    enc["alg"] = "aes-256-gcm"
    with pytest.raises(OtaError, match="unsupported payload encryption"):
        payload.decrypt_artifact(ciphertext, keys, enc)


@pytest.mark.parametrize("drop", ["wraps", "iv", "size"])
def test_a_malformed_enc_block_is_refused(drop):
    keys = {1: payload.new_key()}
    ciphertext, enc = payload.encrypt_artifact(IMAGE, keys)
    del enc[drop]
    with pytest.raises(OtaError, match="malformed payload encryption block"):
        payload.decrypt_artifact(ciphertext, keys, enc)


def test_a_rotation_publishes_under_both_keys_until_the_fleet_has_moved():
    """The new board key only reaches a camera as firmware, so the release that
    carries a fleet through a rotation has to be installable by both halves."""
    old, new = payload.new_key(), payload.new_key()
    ciphertext, enc = payload.encrypt_artifact(IMAGE, {1: old, 2: new})
    assert sorted(enc["wraps"]) == ["1", "2"]
    assert payload.decrypt_artifact(ciphertext, {1: old}, enc) == IMAGE          # not updated yet
    assert payload.decrypt_artifact(ciphertext, {1: old, 2: new}, enc) == IMAGE  # updated
    # and a camera that has both uses the NEWER one, so dropping the old wrap from
    # later releases is a non-event
    assert payload.select_wrap(enc["wraps"], {1: old, 2: new})[0] == 2


def test_an_artifact_encrypted_for_nobody_we_are_says_so():
    ciphertext, enc = payload.encrypt_artifact(IMAGE, {2: payload.new_key()})
    with pytest.raises(OtaError, match="not encrypted for any key we hold"):
        payload.decrypt_artifact(ciphertext, {1: payload.new_key()}, enc)


def test_encrypting_for_no_key_at_all_is_a_mistake_not_a_plaintext_release():
    with pytest.raises(OtaError, match="no board key to encrypt for"):
        payload.encrypt_artifact(IMAGE, {})

"""The Alif Ensemble's OSPI decryption engine, as the stub and the firmware module run it in
software.

``stub/ports/alif/ecb_slot.c`` (and the AES-128 decryption beside the stub's encryption) is
compiled for the host and held to what the silicon read (bench, OPENMV_AE3, 2026-11-13: an
erased block through the engine, under two keys loaded through the Secure Enclave), and to
the host's twins in ``slot_crypt`` everywhere else. Skipped without gcc.
"""

import os
import shutil
import subprocess

import pytest

from openmv_ota.build import stub as stub_mod
from openmv_ota.build.device import openmv_slot_engine as engine
from openmv_ota.ota import slot_crypt

pytestmark = pytest.mark.skipif(not shutil.which("gcc"), reason="needs gcc to compile the stub's cipher")

_AES_C = stub_mod.STUB_DIR / "lib" / "aes" / "aes128.c"
_ECB_C = stub_mod.STUB_DIR / "ports" / "alif" / "ecb_slot.c"

# The bench: the 16 bytes handed to the Secure Enclave's key service, and what sixteen 0xFF
# bytes of erased flash then read as through the memory map.
_BENCH_READS = [
    (bytes(range(16)), "619651df60a3a4dbd2dad946759a889f"),
    (bytes(16), "9c2bd23823b437090f8f1d9ab4796f72"),
]

_HARNESS_C = r"""
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "aes128.h"
#include "ecb_slot.h"

static size_t unhex(const char *h, uint8_t *out) {
    size_t n = strlen(h) / 2;
    for (size_t i = 0; i < n; i++) {
        unsigned v;
        sscanf(h + 2 * i, "%2x", &v);
        out[i] = (uint8_t) v;
    }
    return n;
}

static void show(const uint8_t *p, size_t n) {
    for (size_t i = 0; i < n; i++) printf("%02x", p[i]);
    printf("\n");
}

// rows: "E <key> <data>"   data encrypted under the slot key
//       "D <key> <data>"   data decrypted
//       "K <flash key> <tweak>"   the slot key, then the engine's form of it
//       "F <area> -"       the flash key in a key area's first 256 bytes, or "none"
int main(int argc, char **argv) {
    FILE *f = fopen(argv[1], "r");
    static char op[4], a[20000], b[20000];
    static uint8_t x[10000], y[10000];
    while (fscanf(f, "%3s %19999s %19999s", op, a, b) == 3) {
        size_t na = unhex(a, x), nb = (b[0] == '-') ? 0 : unhex(b, y);
        (void) na;
        if (op[0] == 'E' || op[0] == 'D') {
            ecb_slot_t e;
            ecb_slot_init(&e, x);
            if (op[0] == 'E') ecb_slot_encrypt(&e, y, (uint32_t) nb); else ecb_slot_decrypt(&e, y, (uint32_t) nb);
            show(y, nb);
        } else if (op[0] == 'K') {
            uint8_t key[16], eng[16];
            ecb_slot_key(x, y, key);
            ecb_slot_engine_key(key, eng);
            show(key, 16);
            show(eng, 16);
        } else {
            uint8_t key[16];
            if (ecb_flash_key(x, key)) show(key, 16); else printf("none\n");
        }
    }
    fclose(f);
    return 0;
}
"""


def _engine_read(service_key, stored):
    """What the engine reads out of one stored block, keyed with ``service_key`` through the
    Secure Enclave: both the key and the block go through AES reversed."""
    return slot_crypt.aes_block(service_key[::-1], stored[::-1], encrypt=False)[::-1]


def test_the_block_layout_reproduces_the_silicon():
    for service_key, read in _BENCH_READS:
        assert _engine_read(service_key, b"\xff" * 16).hex() == read
        # the host twin: the slot key is the service's key reversed
        assert slot_crypt.ecb_decrypt(service_key[::-1], b"\xff" * 16).hex() == read


def test_the_one_block_operation_is_aes():
    # FIPS-197 appendix C.1
    key, plain = bytes(range(16)), bytes.fromhex("00112233445566778899aabbccddeeff")
    stored = slot_crypt.aes_block(key, plain)
    assert stored.hex() == "69c4e0d86a7b0430d8cdb78070b4c55a"
    assert slot_crypt.aes_block(key, stored, encrypt=False) == plain
    for k, b in ((key[:15], plain), (key, plain + plain)):
        with pytest.raises(ValueError, match="16 bytes"):
            slot_crypt.aes_block(k, b)


def test_the_host_twins():
    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    data = os.urandom(160)
    stored = slot_crypt.ecb_encrypt(key, data)
    assert stored != data and slot_crypt.ecb_decrypt(key, stored) == data
    # no address in it: equal blocks are equal wherever they are
    assert slot_crypt.ecb_encrypt(key, data[:16] * 3) == stored[:16] * 3
    tweak = bytes(range(10))
    k = slot_crypt.ecb_slot_key(key, tweak)
    assert k.hex() == "4b6121773c31bb7eb9f52c3869e31f0c"
    assert slot_crypt.ecb_slot_key(key, bytes(10)) != k
    for bad in (lambda: slot_crypt.ecb_encrypt(b"short", data), lambda: slot_crypt.ecb_encrypt(key, data[:15]),
                lambda: slot_crypt.ecb_slot_key(b"short", tweak), lambda: slot_crypt.ecb_slot_key(key, b"x")):
        with pytest.raises(ValueError):
            bad()


def _area(*places):
    area = bytearray(256)
    for i, place in enumerate(places):
        area[i * 64:i * 64 + len(place)] = place
    return bytes(area)


def _place(key, magic=b"OMVF\x01"):
    return magic + bytes(32 - len(magic)) + key + bytes(b ^ 0xFF for b in key)


def test_the_flash_key_is_the_first_whole_place():
    k1, k2 = os.urandom(16), os.urandom(16)
    assert slot_crypt.flash_key(bytes(256)) is None
    assert slot_crypt.flash_key(_area(_place(k1))) == k1
    # a place cut off before its commit word, or with a torn key word, never counts
    torn = bytes(32) + k1 + bytes(16)
    assert slot_crypt.flash_key(_area(torn)) is None
    assert slot_crypt.flash_key(_area(torn, _place(k2))) == k2
    assert slot_crypt.flash_key(_area(b"OMVF\x01" + bytes(27) + k1 + bytes(16), _place(k2))) == k2
    assert slot_crypt.flash_key(_area(_place(k1, b"OMVF\x02"))) is None      # a layout it does not know


class _Store:
    """``key_store`` over a bytearray: blank is 0x00 (MRAM), and only blank bytes are written."""
    BLANK = 0

    def __init__(self):
        self.area = bytearray(4096)
        self.writes = []

    def read(self, off, n):
        return bytes(self.area[off:off + n])

    def write(self, off, data):
        assert off % 32 == 0 and len(data) % 32 == 0 and not any(self.area[off:off + len(data)])
        self.area[off:off + len(data)] = data
        self.writes.append(off)


def test_boot_py_makes_the_key_the_stub_reads():
    store = _Store()
    key = os.urandom(16)
    assert engine.make_flash_key(store, lambda n: key[:n]) is True
    assert store.writes == [32, 0]                       # the key word, then the word that commits it
    assert slot_crypt.flash_key(bytes(store.area[:256])) == key
    assert not any(store.area[256:])                     # the identity records' slots are not touched
    # a place a power cut left half written is passed over
    store = _Store()
    store.area[32:48] = os.urandom(16)
    assert engine.make_flash_key(store, lambda n: key[:n]) is True
    assert store.writes == [96, 64] and slot_crypt.flash_key(bytes(store.area[:256])) == key
    # nowhere left
    store = _Store()
    for off in range(0, 256, 64):
        store.area[off] = 1
    assert engine.make_flash_key(store, lambda n: key[:n]) is False and store.writes == []


def test_the_stubs_cipher_matches_the_silicon_and_the_host_twins(tmp_path):
    for src in (_AES_C, _ECB_C):
        shutil.copy2(src, tmp_path / src.name)
    (tmp_path / "harness.c").write_text(_HARNESS_C)
    exe = tmp_path / "ecb"
    subprocess.run(["gcc", "-O1", "-Wall", "-Werror", "-I", str(_AES_C.parent), "-I", str(_ECB_C.parent),
                    str(tmp_path / "harness.c"), str(tmp_path / "aes128.c"),
                    str(tmp_path / "ecb_slot.c"), "-o", str(exe)], check=True)
    key, tweak = os.urandom(16), os.urandom(10)
    data = os.urandom(4096)
    k1 = os.urandom(16)
    store = _Store()
    engine.make_flash_key(store, lambda n: k1[:n])
    rows = [("D", s[::-1], b"\xff" * 16) for s, _r in _BENCH_READS]
    rows += [("E", key, data), ("D", key, slot_crypt.ecb_encrypt(key, data)), ("K", key, tweak),
             ("F", bytes(256), None), ("F", bytes(store.area[:256]), None),
             ("F", _area(bytes(32) + k1 + bytes(16), _place(key)), None)]
    (tmp_path / "rows.txt").write_text(
        "".join("%s %s %s\n" % (op, a.hex(), b.hex() if b is not None else "-") for op, a, b in rows))
    out = subprocess.run([str(exe), str(tmp_path / "rows.txt")], check=True, capture_output=True,
                         text=True).stdout.split("\n")
    assert out[0] == _BENCH_READS[0][1] and out[1] == _BENCH_READS[1][1]
    assert out[2] == slot_crypt.ecb_encrypt(key, data).hex()
    assert out[3] == data.hex()
    slot_key = slot_crypt.ecb_slot_key(key, tweak)
    assert out[4] == slot_key.hex() and out[5] == slot_key[::-1].hex()   # the service takes it reversed
    assert out[6:9] == ["none", k1.hex(), key.hex()]

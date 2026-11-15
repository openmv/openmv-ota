"""The i.MX RT1062 engine's counter mode, as the stub and the firmware module run it in software.

``stub/ports/mimxrt/bee_stream.c`` is compiled for the host and held to keystream read off the
silicon (bench, OPENMV_RT1060, 2026-11-13: an enabled range over erased flash reads back as
the keystream XOR 0xFF), and to the host's twin (``slot_crypt.bee_keystream_xor``) everywhere
else. Skipped without gcc.
"""

import shutil
import struct
import subprocess

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from openmv_ota.build import stub as stub_mod
from openmv_ota.ota import slot_crypt

pytestmark = pytest.mark.skipif(not shutil.which("gcc"), reason="needs gcc to compile the stub's cipher")

_AES_C = stub_mod.STUB_DIR / "lib" / "aes" / "aes128.c"
_STREAM_C = stub_mod.STUB_DIR / "ports" / "mimxrt" / "bee_stream.c"

# The bench. AES_KEY0_W0..3 = 03020100 07060504 0b0a0908 0f0e0d0c (the bytes 00..0f as
# little-endian words): the engine keys with those bytes reversed.
_BENCH_WORDS = (0x03020100, 0x07060504, 0x0B0A0908, 0x0F0E0D0C)
_BENCH_KEY = bytes(range(16))[::-1]
# What an erased block read back as, per (CTR_NONCE W0..W3, address).
_BENCH_READS = [
    ((0, 0, 0, 0), 0x60BE0700, "6d6c3baefdacdae124060857d5442dfb"),
    ((0, 0, 0, 0), 0x60BE0710, "80c473f485e172926cd01cb8ab3f188b"),
    ((0, 0, 0, 0), 0x60BE07F0, "2c4e541791be87fff8e6754dc2d3a949"),
    ((0, 0x22222222, 0x33333333, 0x44444444), 0x60BE2100, "361c36d0c8e14c65d2e4792e5e2d05c6"),
    ((0, 0x22222222, 0x33333333, 0x44444444), 0x60BE27F0, "610141507b83f23006ea33bf4bf86814"),
    ((0, 0x07060504, 0x0B0A0908, 0x0F0E0D0C), 0x60BE2230, "ff0f3d00bad80589efae9163eb0cc6bf"),
]

_HARNESS_C = r"""
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "aes128.h"
#include "bee_stream.h"

static void unhex(const char *h, uint8_t *out) {
    for (size_t i = 0; i < strlen(h) / 2; i++) {
        unsigned v;
        sscanf(h + 2 * i, "%2x", &v);
        out[i] = (uint8_t) v;
    }
}

// rows: "S <key> <tweak> <addr> <n>" -> n keystream bytes from addr (XOR over zeros)
//       "W <key> <tweak> 0 0"        -> AES_KEY0_W0..3, CTR_NONCE W0..3
int main(int argc, char **argv) {
    FILE *f = fopen(argv[1], "r");
    char op[4], a[64], b[64];
    unsigned long addr, n;
    uint8_t key[16], tweak[16];
    static uint8_t buf[70000];
    while (fscanf(f, "%3s %63s %63s %lx %lu", op, a, b, &addr, &n) == 5) {
        unhex(a, key);
        unhex(b, tweak);
        if (op[0] == 'S') {
            bee_stream_t s;
            bee_stream_init(&s, key, tweak);
            memset(buf, 0, n);
            bee_stream_xor(&s, (uint32_t) addr, buf, (uint32_t) n);
            for (size_t i = 0; i < n; i++) printf("%02x", buf[i]);
            printf("\n");
        } else {
            for (unsigned i = 0; i < 4; i++) printf("%08x ", bee_stream_key_word(key, i));
            for (unsigned i = 0; i < 4; i++) printf("%08x ", bee_stream_nonce_word(tweak, i));
            printf("\n");
        }
    }
    fclose(f);
    return 0;
}
"""


def _counter_keystream(key, words, addr):
    """The engine's keystream block, from the nonce words as the registers hold them."""
    ctr = struct.pack(">4I", words[3], words[2], words[1], addr >> 4)
    return Cipher(algorithms.AES(key), modes.CTR(ctr)).encryptor().update(bytes(16))


def test_the_counter_layout_reproduces_the_silicon():
    """Six reads of erased flash through the engine, three nonces: every one is the model's
    keystream XOR 0xFF. (NONCE W0 is zero in all of them, as the stub always leaves it.)"""
    for words, addr, read in _BENCH_READS:
        ks = _counter_keystream(_BENCH_KEY, words, addr)
        assert bytes(b ^ 0xFF for b in ks).hex() == read, (words, hex(addr))


def test_the_host_twin_is_that_layout_with_the_tweak_in_w3_w2_and_half_of_w1():
    tweak = bytes.fromhex("44444444333333332222")
    words = (0, 0x22220000, 0x33333333, 0x44444444)
    for addr in (0x60050000, 0x60BE2100, 0x60FFFFF0):
        assert slot_crypt.bee_keystream_xor(_BENCH_KEY, tweak, addr, bytes(16)) == \
            _counter_keystream(_BENCH_KEY, words, addr)
    # a zero tweak is three of the silicon's reads
    for _words, addr, read in _BENCH_READS[:3]:
        got = slot_crypt.bee_keystream_xor(_BENCH_KEY, bytes(10), addr, b"\xff" * 16)
        assert got.hex() == read
    # unaligned starts and lengths walk the same stream
    whole = slot_crypt.bee_keystream_xor(_BENCH_KEY, tweak, 0x60050000, bytes(100))
    assert slot_crypt.bee_keystream_xor(_BENCH_KEY, tweak, 0x60050007, bytes(41)) == whole[7:48]
    with pytest.raises(ValueError):
        slot_crypt.bee_keystream_xor(b"short", tweak, 0, b"")


def test_the_stubs_stream_matches_the_silicon_and_the_host_twin(tmp_path):
    for src in (_AES_C, _STREAM_C):
        shutil.copy2(src, tmp_path / src.name)
    (tmp_path / "harness.c").write_text(_HARNESS_C)
    exe = tmp_path / "bee"
    subprocess.run(["gcc", "-O1", "-Wall", "-Werror", "-I", str(_AES_C.parent), "-I", str(_STREAM_C.parent),
                    str(tmp_path / "harness.c"), str(tmp_path / "aes128.c"),
                    str(tmp_path / "bee_stream.c"), "-o", str(exe)], check=True)
    tweak = bytes.fromhex("0102030405060708090a")
    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    cases = [(_BENCH_KEY, bytes(10), addr, 16) for _w, addr, _r in _BENCH_READS[:3]]
    cases += [(key, tweak, 0x60050000, 48), (key, tweak, 0x60050003, 29), (key, tweak, 0x60FFFFF8, 8),
              (key, tweak, 0x60800000, 4096 + 5)]
    rows = "".join("S %s %s %x %d\n" % (k.hex(), t.hex(), a, n) for k, t, a, n in cases)
    rows += "W %s %s 0 0\n" % (_BENCH_KEY.hex(), tweak.hex())
    (tmp_path / "rows.txt").write_text(rows)
    out = subprocess.run([str(exe), str(tmp_path / "rows.txt")], check=True, capture_output=True,
                         text=True).stdout.split("\n")
    for (k, t, a, n), line in zip(cases, out, strict=False):
        assert line == slot_crypt.bee_keystream_xor(k, t, a, bytes(n)).hex(), hex(a)
    for (_w, _addr, read), line in zip(_BENCH_READS[:3], out, strict=False):
        assert bytes(b ^ 0xFF for b in bytes.fromhex(line)).hex() == read
    words = [int(w, 16) for w in out[len(cases)].split()]
    assert tuple(words[:4]) == _BENCH_WORDS                 # the registers the bench key went in as
    assert words[4:] == [0, 0x090A0000, 0x05060708, 0x01020304]   # W0 zero, the tweak in W3 W2 W1

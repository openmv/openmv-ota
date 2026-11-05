"""The boot stub's flash cipher, compiled for the host and held to the silicon.

``stub/lib/aes/aes128.c`` and ``stub/ports/stm32/mce_stream.c`` are the STM32N6 memory cipher
engine's AES-128 stream mode in software: what the stub encrypts a firmware image with before
programming it into a region the engine decrypts on the way out. Nothing here can be checked
against a datasheet -- the reference manual gives the counter's fields, not their byte order,
nor that the engine reverses each keystream block -- so the vectors are what an OPENMV_N6
produced on the bench (2026-11-04: zeros programmed raw, read back through an MCE2 stream
region). The rest is checked against the host's own AES.

Skipped without gcc/gcov.
"""
from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from openmv_ota.build import stub as stub_mod

pytestmark = pytest.mark.skipif(not (shutil.which("gcc") and shutil.which("gcov")),
                                reason="needs gcc and gcov to compile the stub's cipher")

_AES_C = stub_mod.STUB_DIR / "lib" / "aes" / "aes128.c"
_STREAM_C = stub_mod.STUB_DIR / "ports" / "stm32" / "mce_stream.c"

# The bench: CCKEYR0..3 = 00010203 04050607 08090a0b 0c0d0e0f, CCNR0 = a5a5a5a5, CCNR1 =
# c3c3c3c3, VERSION = fedc, a region at 0x70800000 over erased-then-zeroed flash.
_BENCH_WORDS = (0x00010203, 0x04050607, 0x08090A0B, 0x0C0D0E0F)
_BENCH_KEY = bytes.fromhex("0c0d0e0f08090a0b0405060700010203")
_BENCH_TWEAK = bytes.fromhex("c3c3c3c3a5a5a5a5fedc")
_BENCH_KEYCRC = 0x95
_BENCH_KS0 = bytes.fromhex("537de0731e6e0a5a301a284ddc09480e561da54c97386be495117b034a6bfed0"
                           "76f49622ade05eea69f00e9563021863")       # 0x70800000, 48 bytes
_BENCH_KS4K = bytes.fromhex("8816a7f2507db55f087a871db8620770")       # 0x70801000

_HARNESS_C = r"""
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "aes128.h"
#include "mce_stream.h"

static size_t unhex(const char *h, uint8_t *out) {
    size_t n = strlen(h) / 2;
    for (size_t i = 0; i < n; i++) {
        unsigned v;
        sscanf(h + 2 * i, "%2x", &v);
        out[i] = (uint8_t) v;
    }
    return n;
}

static void phex(const uint8_t *b, size_t n) {
    for (size_t i = 0; i < n; i++) printf("%02x", b[i]);
    printf("\n");
}

// rows: "A <key> <block>"            -> AES-128(key, block)
//       "S <key> <tweak> <addr> <n>" -> n keystream bytes from addr (XOR over zeros)
//       "W <key> <tweak>"            -> CCKEYR0..3, CCNR0, CCNR1, VERSION, KEYCRC
int main(int argc, char **argv) {
    FILE *f = fopen(argv[1], "r");
    char op[4], a[64], b[64];
    uint8_t key[16], blk[16], tweak[16];
    static uint8_t buf[70000];
    while (fscanf(f, "%3s %63s %63s", op, a, b) == 3) {
        unhex(a, key);
        if (op[0] == 'A') {
            aes128_t aes;
            unhex(b, blk);
            aes128_init(&aes, key);
            aes128_encrypt(&aes, blk, blk);
            phex(blk, 16);
        } else if (op[0] == 'S') {
            unsigned long addr, n;
            mce_stream_t m;
            fscanf(f, "%lx %lu", &addr, &n);
            unhex(b, tweak);
            mce_stream_init(&m, key, tweak);
            memset(buf, 0, n);
            mce_stream_xor(&m, (uint32_t) addr, buf, (uint32_t) n);
            phex(buf, n);
        } else {
            uint32_t w[4];
            unhex(b, tweak);
            for (unsigned i = 0; i < 4; i++) w[i] = mce_stream_key_word(key, i);
            printf("%08x %08x %08x %08x %08x %08x %04x %02x\n", w[0], w[1], w[2], w[3],
                   mce_stream_nonce_word(tweak, 0), mce_stream_nonce_word(tweak, 1),
                   mce_stream_version(tweak), mce_stream_key_crc(w));
        }
    }
    fclose(f);
    return 0;
}
"""


def _aes(key, block):
    return Cipher(algorithms.AES(key), modes.ECB()).encryptor().update(block)


def keystream(key, tweak, addr, n):
    """The engine's keystream for ``n`` bytes from ``addr``, on the host's AES: the twin the C
    is checked against once the bench vectors have pinned the layout."""
    out = bytearray()
    first = addr & ~15
    for a in range(first, addr + n, 16):
        ctr = tweak[:8] + b"\x00\x00" + tweak[8:10] + struct.pack(">I", (a >> 4) & 0x0FFFFFFF)
        out += _aes(key, ctr)[::-1]
    return bytes(out[addr - first:addr - first + n])


def test_the_host_twin_reproduces_the_silicon():
    assert keystream(_BENCH_KEY, _BENCH_TWEAK, 0x70800000, 48) == _BENCH_KS0
    assert keystream(_BENCH_KEY, _BENCH_TWEAK, 0x70801000, 16) == _BENCH_KS4K


def test_the_stubs_cipher_matches_the_silicon_and_the_hosts_aes(tmp_path):
    for src in (_AES_C, _STREAM_C):
        shutil.copy2(src, tmp_path / src.name)
    (tmp_path / "harness.c").write_text(_HARNESS_C)
    cflags = ["--coverage", "-O0", "-Wall", "-Werror", "-I", str(_AES_C.parent),
              "-I", str(_STREAM_C.parent)]
    for src in ("aes128.c", "mce_stream.c", "harness.c"):
        subprocess.run(["gcc", *cflags, "-c", src, "-o", src[:-2] + ".o"], cwd=tmp_path, check=True)
    subprocess.run(["gcc", "--coverage", "aes128.o", "mce_stream.o", "harness.o", "-o", "harness"],
                   cwd=tmp_path, check=True)

    rows, want = [], []
    # FIPS 197 appendix C.1, then random blocks against the host's AES
    fips_key = bytes(range(16))
    rows.append("A %s %s" % (fips_key.hex(), bytes.fromhex("00112233445566778899aabbccddeeff").hex()))
    want.append("69c4e0d86a7b0430d8cdb78070b4c55a")
    for _ in range(20):
        k, b = os.urandom(16), os.urandom(16)
        rows.append("A %s %s" % (k.hex(), b.hex()))
        want.append(_aes(k, b).hex())
    # the bench: the keystream, and the register values that produced it
    rows.append("S %s %s %x %d" % (_BENCH_KEY.hex(), _BENCH_TWEAK.hex(), 0x70800000, 48))
    want.append(_BENCH_KS0.hex())
    rows.append("S %s %s %x %d" % (_BENCH_KEY.hex(), _BENCH_TWEAK.hex(), 0x70801000, 16))
    want.append(_BENCH_KS4K.hex())
    rows.append("W %s %s" % (_BENCH_KEY.hex(), _BENCH_TWEAK.hex()))
    want.append("%08x %08x %08x %08x a5a5a5a5 c3c3c3c3 fedc %02x" % (*_BENCH_WORDS, _BENCH_KEYCRC))
    # any address, any length: unaligned starts and ends, a block boundary, a whole erase block
    for addr, n in ((0x70090000, 1), (0x70090007, 3), (0x7009000F, 2), (0x700FFFF1, 4096 + 30),
                    (0x703EF000, 65536), (0x7FFFFFF0, 16)):
        k, t = os.urandom(16), os.urandom(10)
        rows.append("S %s %s %x %d" % (k.hex(), t.hex(), addr, n))
        want.append(keystream(k, t, addr, n).hex())

    (tmp_path / "rows.txt").write_text("".join(r + "\n" for r in rows))
    run = subprocess.run([str(tmp_path / "harness"), "rows.txt"], cwd=tmp_path,
                         capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
    got = run.stdout.splitlines()
    assert len(got) == len(want)
    for row, g, w in zip(rows, got, want, strict=True):
        assert g == w, row

    for src in ("aes128.c", "mce_stream.c"):
        gcov = subprocess.run(["gcov", "-n", src], cwd=tmp_path, capture_output=True, text=True)
        m = re.search(r"Lines executed:([\d.]+)% of \d+", gcov.stdout)
        assert m, gcov.stdout + gcov.stderr
        assert m.group(1) == "100.00", "%s not fully covered:\n%s" % (src, gcov.stdout)


def test_the_cipher_sources_ship_with_the_stub():
    assert _AES_C.is_file() and (_AES_C.parent / "aes128.h").is_file()
    assert _STREAM_C.is_file() and (_STREAM_C.parent / "mce_stream.h").is_file()

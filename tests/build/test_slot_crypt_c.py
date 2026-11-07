"""The slot cipher's two C pieces agree with their Python twins.

* ``common/slot_tweak.c`` -- the tweak the stub ciphers a slot under -- against
  :func:`openmv_ota.ota.slot_crypt.slot_tweak` and the installer's ``_slot_tweak``;
* ``device/flash_crypt.c``'s core (``omv_flash_crypt_xor``, the installer's keystream, pasted
  together with the stub's AES and stream sources exactly as ``build firmware`` does) against
  :func:`openmv_ota.ota.slot_crypt.keystream_xor`.

Skipped without gcc.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from openmv_ota.build import firmware as fw
from openmv_ota.build import stub as stub_mod
from openmv_ota.ota import slot_crypt

pytestmark = pytest.mark.skipif(not shutil.which("gcc"), reason="needs gcc to compile the stub's cipher")

_SHA_C = stub_mod.STUB_DIR / "lib" / "sha256" / "sha256.c"
_TWEAK_C = stub_mod.STUB_DIR / "common" / "slot_tweak.c"

_HARNESS_C = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include "slot_tweak.h"

int omv_flash_crypt_xor(const uint8_t key[16], const uint8_t tweak[10], uint32_t addr, uint8_t *buf, size_t len);

static int unhex(const char *s, uint8_t *out, size_t n) {
    for (size_t i = 0; i < n; i++) {
        unsigned v;
        if (sscanf(s + 2 * i, "%02x", &v) != 1) return -1;
        out[i] = (uint8_t) v;
    }
    return 0;
}

// Rows: "T <index> <trailer_sha256 hex>" -> tweak hex; "X <key> <tweak> <addr hex> <data hex>" -> data hex
int main(int argc, char **argv) {
    FILE *f = fopen(argv[1], "r");
    char line[16384];
    while (fgets(line, sizeof(line), f)) {
        char kind, a[16384], b[16384], c[16384], d[16384];
        unsigned long idx;
        if (line[0] == 'T') {
            if (sscanf(line, "T %lu %s", &idx, a) != 2) return 1;
            uint8_t sha[32], out[10];
            if (unhex(a, sha, 32) != 0) return 1;
            slot_tweak_derive((uint32_t) idx, sha, out);
            for (int i = 0; i < 10; i++) printf("%02x", out[i]);
            printf("\n");
        } else if (line[0] == 'X') {
            if (sscanf(line, "%c %s %s %s %s", &kind, a, b, c, d) != 5) return 1;
            uint8_t key[16], tweak[10], buf[8192];
            size_t n = strlen(d) / 2;
            if (unhex(a, key, 16) != 0 || unhex(b, tweak, 10) != 0 || unhex(d, buf, n) != 0) return 1;
            omv_flash_crypt_xor(key, tweak, (uint32_t) strtoul(c, NULL, 16), buf, n);
            for (size_t i = 0; i < n; i++) printf("%02x", buf[i]);
            printf("\n");
        } else {
            return 1;
        }
    }
    return 0;
}
"""


def _installer():
    path = Path(fw.__file__).resolve().parent / "device" / "openmv_ota" / "data" / "installer.py"
    spec = importlib.util.spec_from_file_location("installer_for_slot_crypt", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_installers_tweak_is_the_hosts():
    inst = _installer()
    for index in (0, 1):
        for _ in range(5):
            h = os.urandom(32)
            assert inst._slot_tweak(index, h) == slot_crypt.slot_tweak(index, h)
    assert slot_crypt.slot_tweak(0, bytes(32)) != slot_crypt.slot_tweak(1, bytes(32))
    with pytest.raises(ValueError):
        slot_crypt.slot_tweak(0, b"short")


def test_the_stubs_tweak_and_the_modules_keystream_match_the_host(tmp_path):
    # the module as `build firmware` writes it: the stub's cipher sources, then the module
    (tmp_path / "flash_crypt.c").write_text(fw._flash_crypt_sources())
    for src in (_SHA_C, _TWEAK_C):
        shutil.copy2(src, tmp_path / src.name)
    shutil.copy2(_SHA_C.with_suffix(".h"), tmp_path / "sha256.h")
    shutil.copy2(_TWEAK_C.with_suffix(".h"), tmp_path / "slot_tweak.h")
    (tmp_path / "harness.c").write_text(_HARNESS_C)
    cflags = ["-O0", "-Wall", "-Werror", "-DOMV_FLASH_CRYPT_HOST_TEST", "-I", str(tmp_path)]
    objs = []
    for src in ("sha256.c", "slot_tweak.c", "flash_crypt.c", "harness.c"):
        subprocess.run(["gcc", *cflags, "-c", src, "-o", src[:-2] + ".o"], cwd=tmp_path, check=True)
        objs.append(src[:-2] + ".o")
    subprocess.run(["gcc", *objs, "-o", "harness"], cwd=tmp_path, check=True)

    rows, want = [], []
    for index in (0, 1, 7):
        for _ in range(4):
            h = os.urandom(32)
            rows.append("T %d %s" % (index, h.hex()))
            want.append(slot_crypt.slot_tweak(index, h).hex())
    for addr, n in ((0x71400000, 16), (0x71400007, 3), (0x7140FFF0, 48), (0x72000000 - 4096, 4096),
                    (0x70800010, 1)):
        k, t, data = os.urandom(16), os.urandom(10), os.urandom(n)
        rows.append("X %s %s %x %s" % (k.hex(), t.hex(), addr, data.hex()))
        want.append(slot_crypt.keystream_xor(k, t, addr, data).hex())
    (tmp_path / "rows.txt").write_text("".join(r + "\n" for r in rows))
    run = subprocess.run([str(tmp_path / "harness"), "rows.txt"], cwd=tmp_path,
                         capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
    got = run.stdout.splitlines()
    assert len(got) == len(want)
    for row, g, w in zip(rows, got, want, strict=True):
        assert g == w, row


def test_the_hosts_keystream_is_the_engines():
    """The host twin here agrees with the bench-pinned one in test_stub_crypt_c."""
    from tests.build.test_stub_crypt_c import _BENCH_KEY, _BENCH_TWEAK, keystream

    for addr, n in ((0x70800000, 48), (0x70801000, 16), (0x7009000F, 40)):
        data = os.urandom(n)
        ks = keystream(_BENCH_KEY, _BENCH_TWEAK, addr, n)
        assert slot_crypt.keystream_xor(_BENCH_KEY, _BENCH_TWEAK, addr, data) == bytes(
            a ^ b for a, b in zip(data, ks, strict=True))
    with pytest.raises(ValueError):
        slot_crypt.keystream_xor(b"k", bytes(10), 0, b"x")

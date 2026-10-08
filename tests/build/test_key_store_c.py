"""Host test for the key-store C module's checks (``device/key_store.c``).

The firmware build gives it a real flash backend per port; here ``OMV_KEY_STORE_HOST_TEST``
swaps that for a 256-byte buffer, so the part that decides what may be written -- whole flash
words, inside the area, onto blank flash only -- runs and is measured on the host.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from openmv_ota.build import firmware as fw

_HAVE_CC = bool(shutil.which("gcc") and shutil.which("gcov"))

_HARNESS_C = r"""
#include <stdint.h>
#include <stdio.h>
#include <string.h>

uint8_t omv_key_store_host_area[256];
#define AREA omv_key_store_host_area
static int fail_program;

int omv_key_store_program(uintptr_t addr, const uint8_t *src, size_t len) {
    if (fail_program) return -1;
    memcpy((void *)addr, src, len);
    return 0;
}

extern int omv_key_store_write(size_t, const uint8_t *, size_t);

int main(void) {
    uint8_t data[256];
    memset(AREA, 0xFF, sizeof(AREA));
    memset(data, 0x5A, sizeof(data));
    printf("%d\n", omv_key_store_write(0, data, 0));      // nothing to write
    printf("%d\n", omv_key_store_write(16, data, 32));    // not on a flash word
    printf("%d\n", omv_key_store_write(0, data, 48));     // not whole flash words
    printf("%d\n", omv_key_store_write(288, data, 32));   // starts past the area
    printf("%d\n", omv_key_store_write(224, data, 64));   // runs past the area
    printf("%d\n", omv_key_store_write(0, data, 96));     // fine
    printf("%d\n", AREA[0] == 0x5A && AREA[95] == 0x5A && AREA[96] == 0xFF);
    printf("%d\n", omv_key_store_write(64, data, 64));    // over what's written: refused
    printf("%d\n", AREA[96] == 0xFF);                     // ...and nothing written
    fail_program = 1;
    printf("%d\n", omv_key_store_write(128, data, 32));   // the flash refuses
    return 0;
}
"""


@pytest.mark.skipif(not _HAVE_CC, reason="needs gcc and gcov")
def test_key_store_writes_only_whole_words_onto_blank_flash_in_the_area(tmp_path):
    shutil.copy2(fw._KEY_STORE_C, tmp_path / "key_store.c")
    (tmp_path / "harness.c").write_text(_HARNESS_C)
    cflags = ["-DOMV_KEY_STORE_HOST_TEST",
              "-DOMV_KEY_AREA_ADDR=((uintptr_t)omv_key_store_host_area)",
              "--coverage", "-O0", "-Wall", "-Werror", "-include", "stdint.h"]
    subprocess.run(["gcc", *cflags, "-c", "key_store.c", "-o", "key_store.o"],
                   cwd=tmp_path, check=True)
    subprocess.run(["gcc", "-O0", "-c", "harness.c", "-o", "harness.o"], cwd=tmp_path, check=True)
    subprocess.run(["gcc", "--coverage", "key_store.o", "harness.o", "-o", "harness"],
                   cwd=tmp_path, check=True)
    out = subprocess.run([str(tmp_path / "harness")], capture_output=True, text=True,
                         check=True).stdout.split()
    assert out == ["-22", "-22", "-22", "-22", "-22", "0", "1", "-17", "1", "-5"]

    gcov = subprocess.run(["gcov", "-n", "key_store.c"], cwd=tmp_path,
                          capture_output=True, text=True)
    m = re.search(r"Lines executed:([\d.]+)% of \d+", gcov.stdout)
    assert m and m.group(1) == "100.00", gcov.stdout + gcov.stderr


def test_key_store_needs_its_area_defined(tmp_path):
    """Built without the address the firmware build adds, it refuses to compile at all."""
    if not shutil.which("gcc"):
        pytest.skip("needs gcc")
    shutil.copy2(fw._KEY_STORE_C, tmp_path / "key_store.c")
    r = subprocess.run(["gcc", "-DOMV_KEY_STORE_HOST_TEST", "-c", "key_store.c"],
                       cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode != 0 and "OMV_KEY_AREA_ADDR not defined" in r.stderr

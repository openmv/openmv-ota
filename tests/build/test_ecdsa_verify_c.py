"""Host test for the ECDSA verify C shim (``device/ecdsa_verify.c``).

The shim's crypto core (``omv_ecdsa_verify``) is pure C, so it is compiled here
against the firmware's *own* mbedtls (3.6.2) and exercised directly: vectors are
**signed by the host ``cryptography`` (OpenSSL) and verified by the shim's mbedtls**
-- proving the host signer and the device verifier agree -- plus tamper / wrong-key
/ wrong-length / unknown-alg / off-curve negatives. ``gcov`` then asserts 100% line
coverage of the core. The MicroPython binding (``mp_obj`` glue) is compiled out via
``OMV_ECDSA_VERIFY_HOST_TEST`` and is exercised on-device (QEMU) instead.

Skipped unless ``OPENMV_FW`` points at an openmv checkout (for the mbedtls source)
and a C toolchain is present.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from openmv_ota.build import firmware as fw
from openmv_ota.ota import keys, sign
from openmv_ota.ota.algorithms import ES256, ES384, ES512, algorithm_for

_FW = os.environ.get("OPENMV_FW")
_MBEDTLS = Path(_FW) / "lib" / "micropython" / "lib" / "mbedtls" if _FW else None
_HAVE_CC = bool(shutil.which("gcc") and shutil.which("gcov"))
_HAVE_MBEDTLS = bool(_MBEDTLS and (_MBEDTLS / "include" / "mbedtls" / "ecdsa.h").exists())

# The whole file needs a compiler. The full crypto/coverage test additionally needs
# the firmware's mbedtls (set OPENMV_FW); the "compiles without mbedtls" guard test
# does not -- it deliberately builds the module with no mbedtls at all.
pytestmark = pytest.mark.skipif(not shutil.which("gcc"), reason="needs gcc to compile the shim")

_NEEDS_MBEDTLS = pytest.mark.skipif(
    not (_HAVE_CC and _HAVE_MBEDTLS),
    reason="set OPENMV_FW to an openmv checkout (for mbedtls) and have gcc/gcov to run",
)

_HARNESS_C = r"""
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
extern int omv_ecdsa_verify(int, const uint8_t *, size_t, const uint8_t *, size_t,
                            const uint8_t *, size_t);

static size_t unhex(const char *h, uint8_t *out) {
    size_t n = strlen(h) / 2;
    for (size_t i = 0; i < n; i++) {
        unsigned v;
        sscanf(h + 2 * i, "%2x", &v);
        out[i] = (uint8_t)v;
    }
    return n;
}

int main(int argc, char **argv) {
    FILE *f = fopen(argv[1], "r");
    if (!f) return 2;
    char alg[16], ph[600], sh[600], mh[4096];
    int want, fails = 0;
    uint8_t pub[300], sig[300], msg[2048];
    while (fscanf(f, "%15s %599s %599s %4095s %d", alg, ph, sh, mh, &want) == 5) {
        int got = omv_ecdsa_verify(atoi(alg), pub, unhex(ph, pub), sig, unhex(sh, sig),
                                   msg, unhex(mh, msg));
        if (got != want) {
            printf("MISMATCH alg=%s want=%d got=%d\n", alg, want, got);
            fails++;
        }
    }
    fclose(f);
    return fails ? 1 : 0;
}
"""


def _vectors():
    """(alg, pubkey, sig, msg, expected) rows that exercise every core branch."""
    msg = b"openmv-ota signed region under test"
    rows = []
    # one valid + one tampered per curve (covers ES256/384/512 mapping + the happy path)
    for cose in (ES256, ES384, ES512):
        spec = algorithm_for(cose)
        priv = keys.generate_private_key(spec)
        pub = bytes.fromhex(keys.public_point_hex(priv.public_key()))
        sig = sign.sign_region(priv, msg, spec)
        rows.append((cose, pub, sig, msg, 1))
        rows.append((cose, pub, sig, msg + b"!", 0))           # tampered message
    # the structural negatives, on ES256
    spec = algorithm_for(ES256)
    priv = keys.generate_private_key(spec)
    pub = bytes.fromhex(keys.public_point_hex(priv.public_key()))
    sig = sign.sign_region(priv, msg, spec)
    other = bytes.fromhex(keys.public_point_hex(keys.generate_private_key(spec).public_key()))
    bad_sig = bytes([sig[0] ^ 0xFF]) + sig[1:]
    off_curve = pub[:40] + bytes([pub[40] ^ 0xFF]) + pub[41:]   # corrupt a coordinate
    rows += [
        (ES256, other, sig, msg, 0),         # valid structure, wrong key
        (ES256, pub, bad_sig, msg, 0),       # tampered signature
        (ES256, off_curve, sig, msg, 0),     # point not on the curve
        (ES256, pub[:-1], sig, msg, 0),      # wrong pubkey length
        (ES256, pub, sig[:-1], msg, 0),      # wrong signature length
        (-8, pub, sig, msg, 0),              # unknown COSE alg (EdDSA)
    ]
    return rows


@_NEEDS_MBEDTLS
def test_ecdsa_verify_c_shim(tmp_path):
    lib = _MBEDTLS / "library" / "libmbedcrypto.a"
    if not lib.exists():       # build the firmware's mbedtls for the host (once)
        r = subprocess.run(["make", "-C", str(_MBEDTLS / "library"), "libmbedcrypto.a"],
                           capture_output=True, text=True)
        if r.returncode != 0:  # surface why -- mbedtls 3.6 needs its `framework`
            raise AssertionError(  # submodule (+ jinja2) to generate sources
                "host mbedtls build failed (need the mbedtls 'framework' submodule and "
                "jinja2):\n" + r.stdout + r.stderr)

    shutil.copy2(fw._VERIFY_C, tmp_path / "ecdsa_verify.c")
    (tmp_path / "harness.c").write_text(_HARNESS_C)
    (tmp_path / "vec.txt").write_text("".join(
        "%d %s %s %s %d\n" % (a, p.hex(), s.hex(), m.hex(), e) for a, p, s, m, e in _vectors()))

    cflags = ["-DOMV_ECDSA_VERIFY_HOST_TEST", "--coverage", "-O0", "-Wall",
              "-I", str(_MBEDTLS / "include")]
    for src in ("ecdsa_verify.c", "harness.c"):
        subprocess.run(["gcc", *cflags, "-c", src, "-o", src[:-2] + ".o"],
                       cwd=tmp_path, check=True)
    subprocess.run(["gcc", "--coverage", "ecdsa_verify.o", "harness.o", str(lib),
                    "-o", "harness"], cwd=tmp_path, check=True)

    run = subprocess.run([str(tmp_path / "harness"), str(tmp_path / "vec.txt")],
                         cwd=tmp_path, capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr     # every vector matched

    gcov = subprocess.run(["gcov", "-n", "ecdsa_verify.c"], cwd=tmp_path,
                          capture_output=True, text=True)
    m = re.search(r"Lines executed:([\d.]+)% of \d+", gcov.stdout)
    assert m, gcov.stdout + gcov.stderr
    assert m.group(1) == "100.00", "core not fully covered:\n" + gcov.stdout


def test_ecdsa_verify_c_empty_without_mbedtls(tmp_path):
    """A core that doesn't build mbedtls (e.g. the AE3 M55_HE helper core) compiles
    this module with no mbedtls define and no mbedtls on the include path. The guard
    must make it an empty translation unit so the build doesn't break -- regression
    test for the AE3 dual-core compile failure."""
    src = tmp_path / "ecdsa_verify.c"
    shutil.copy2(fw._VERIFY_C, src)
    obj = tmp_path / "ecdsa_verify.o"
    r = subprocess.run(
        ["gcc", "-c", "-O0", "-Wall", "-Werror", str(src), "-o", str(obj)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, "no-mbedtls build must succeed:\n" + r.stderr
    nm = subprocess.run(["nm", str(obj)], capture_output=True, text=True)
    assert "omv_ecdsa_verify" not in nm.stdout, "module not compiled out:\n" + nm.stdout
    assert "ecdsa_verify_module" not in nm.stdout, "binding not compiled out:\n" + nm.stdout

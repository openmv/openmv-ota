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

extern int omv_ecdsa_public_key(const uint8_t *, size_t, uint8_t *, const uint8_t *, size_t);
extern int omv_ecdsa_sign(const uint8_t *, size_t, const uint8_t *, size_t, uint8_t *,
                          const uint8_t *, size_t);
extern int omv_ecdh(const uint8_t *, size_t, const uint8_t *, size_t, uint8_t *,
                    const uint8_t *, size_t);

static void phex(const char *tag, const uint8_t *b, size_t n) {
    printf("%s ", tag);
    for (size_t i = 0; i < n; i++) printf("%02x", b[i]);
    printf("\n");
}

// sign.txt rows: "P <priv> <entropy>", "S <priv> <digest> <entropy>" or "E <priv> <peer>
// <entropy>"; each prints its result (or "P -" / "S -" / "E -" on refusal) for the Python side
// to check with the host's own crypto.
static void sign_rows(const char *path) {
    FILE *f = fopen(path, "r");
    char op[4], a[200], b[200], c[400];
    uint8_t priv[100], dg[100], ent[200], out[65];
    while (fscanf(f, "%3s %199s", op, a) == 2) {
        size_t np = unhex(a, priv);
        if (op[0] == 'P') {
            fscanf(f, "%399s", c);
            if (omv_ecdsa_public_key(priv, np, out, ent, unhex(c, ent))) phex("P", out, 65);
            else printf("P -\n");
        } else if (op[0] == 'E') {
            fscanf(f, "%199s %399s", b, c);
            size_t nq = unhex(b, dg);
            if (omv_ecdh(priv, np, dg, nq, out, ent, unhex(c, ent))) phex("E", out, 32);
            else printf("E -\n");
        } else {
            fscanf(f, "%199s %399s", b, c);
            size_t nd = unhex(b, dg);
            if (omv_ecdsa_sign(priv, np, dg, nd, out, ent, unhex(c, ent))) phex("S", out, 64);
            else printf("S -\n");
        }
    }
    fclose(f);
}

int main(int argc, char **argv) {
    if (argc > 2) sign_rows(argv[2]);
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


def _sign_rows():
    """Rows for the C signer and, per row, a check of what it printed -- each against the
    host's own crypto: the public key must be the host's, the signature must verify."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import (Prehashed,
                                                                 encode_dss_signature)
    rows, checks = [], []
    ent = os.urandom(160).hex()
    for _ in range(3):
        key = ec.generate_private_key(ec.SECP256R1())
        priv = key.private_numbers().private_value.to_bytes(32, "big")
        pub = key.public_key().public_bytes(serialization.Encoding.X962,
                                            serialization.PublicFormat.UncompressedPoint)
        digest = os.urandom(32)
        rows.append("P %s %s" % (priv.hex(), ent))
        checks.append(lambda out, pub=pub: _eq(bytes.fromhex(out), pub))

        def verify(out, key=key, digest=digest):
            rs = bytes.fromhex(out)
            der = encode_dss_signature(int.from_bytes(rs[:32], "big"),
                                       int.from_bytes(rs[32:], "big"))
            key.public_key().verify(der, digest, ec.ECDSA(Prehashed(hashes.SHA256())))
        rows.append("S %s %s %s" % (priv.hex(), digest.hex(), ent))
        checks.append(verify)
    n = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
    refused = lambda out: _eq(out, "-")                        # noqa: E731
    good = priv.hex()
    for bad in ("00" * 32, n.to_bytes(32, "big").hex(), good[:-2]):   # 0, n, 31 bytes
        rows.append("P %s %s" % (bad, ent))
        checks.append(refused)
    rows += ["S %s %s %s" % (good, "00" * 31, ent),            # a 31-byte digest
             "S %s %s %s" % (good, "00" * 32, "00"),           # not enough entropy to sign
             "P %s %s" % (good, "00")]                         # ...or to blind the multiply
    checks += [refused, refused, refused]
    # ECDH: the C side's secret must be the one the host's own ECDH derives from the other end
    peer = ec.generate_private_key(ec.SECP256R1())
    peer_pub = peer.public_key().public_bytes(serialization.Encoding.X962,
                                              serialization.PublicFormat.UncompressedPoint)
    want = peer.exchange(ec.ECDH(), key.public_key())
    rows.append("E %s %s %s" % (good, peer_pub.hex(), ent))
    checks.append(lambda out, want=want: _eq(bytes.fromhex(out), want))
    off_curve = peer_pub[:40] + bytes([peer_pub[40] ^ 0xFF]) + peer_pub[41:]
    rows += ["E %s %s %s" % (good, off_curve.hex(), ent),       # a peer point off the curve
             "E %s %s %s" % (good, peer_pub[:-1].hex(), ent),   # a short peer point
             "E %s %s %s" % ("00" * 32, peer_pub.hex(), ent),   # a bad private key
             "E %s %s %s" % (good, peer_pub.hex(), "00")]       # not enough entropy to blind
    checks += [refused] * 4
    return rows, checks


def _eq(a, b):
    assert a == b


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

    rows, checks = _sign_rows()
    (tmp_path / "sign.txt").write_text("".join(r + "\n" for r in rows))
    run = subprocess.run([str(tmp_path / "harness"), str(tmp_path / "vec.txt"),
                          str(tmp_path / "sign.txt")],
                         cwd=tmp_path, capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr     # every vector matched
    outs = [ln for ln in run.stdout.splitlines() if ln[:2] in ("P ", "S ", "E ")]
    assert len(outs) == len(checks)
    for line, check in zip(outs, checks, strict=True):
        check(line[2:])

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

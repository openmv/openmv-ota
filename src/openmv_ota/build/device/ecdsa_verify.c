// SPDX-License-Identifier: MIT
//
// ecdsa_verify -- ECDSA-over-mbedtls signature verify for the OTA boot.py.
//
// Exposes to MicroPython:
//
//   ecdsa_verify.verify(alg, pubkey, sig, msg) -> bool
//     alg    : COSE algorithm id (-7 ES256 / -35 ES384 / -36 ES512)
//     pubkey : uncompressed EC public point, 04 || X || Y
//     sig    : raw R || S signature (fixed width for the curve)
//     msg    : the trailer's signed region; hashed here with the alg's hash
//   ecdsa_verify.public_key(priv, entropy) -> 65 bytes, 04 || X || Y   (P-256)
//   ecdsa_verify.sign(priv, digest, entropy) -> 64 bytes, R || S        (P-256)
//     the camera's own key, on a board with no secure element (see openmv_ota.se)
//
// It reuses the firmware's already-compiled mbedtls (ECDSA + the NIST P-curves +
// SHA-256/384/512 -- the same primitives TLS uses), so there is no bespoke crypto.
// The openmv build auto-compiles every modules/*.c, so `openmv-ota build firmware`
// just drops this file into modules/ for an OTA firmware (and removes it after).
//
// The crypto core ``omv_ecdsa_verify`` is pure C (no MicroPython), so it is
// host-tested against this exact mbedtls (test_ecdsa_verify_c); the MicroPython
// binding below is compiled out of that host build via OMV_ECDSA_VERIFY_HOST_TEST.

#include <stddef.h>
#include <stdint.h>

// Compiled only where mbedtls is available: the host coverage test (which brings
// its own mbedtls via OMV_ECDSA_VERIFY_HOST_TEST) and firmware cores that build
// mbedtls. On a core without it -- e.g. the Alif AE3 M55_HE helper core, which is
// slaved to the main core and never runs OTA -- the mbedtls headers/lib aren't on
// the include path, so this whole unit must be empty. boot.py then finds no
// ecdsa_verify module, treats the core as non-OTA, and keeps the stock romfs mount.
#if defined(OMV_ECDSA_VERIFY_HOST_TEST) || (defined(MICROPY_SSL_MBEDTLS) && MICROPY_SSL_MBEDTLS)

#include "mbedtls/ecdsa.h"
#include "mbedtls/ecp.h"
#include "mbedtls/md.h"
#include "mbedtls/bignum.h"

typedef struct {
    int cose_id;
    mbedtls_ecp_group_id grp_id;
    mbedtls_md_type_t md_type;
    size_t hash_len;   // digest length of md_type
    size_t pub_len;    // uncompressed point length (1 + 2*coord)
    size_t sig_len;    // raw R||S length (2*coord)
} alg_spec_t;

static const alg_spec_t ALGS[] = {
    { -7,  MBEDTLS_ECP_DP_SECP256R1, MBEDTLS_MD_SHA256, 32, 65,  64  },  // ES256
    { -35, MBEDTLS_ECP_DP_SECP384R1, MBEDTLS_MD_SHA384, 48, 97,  96  },  // ES384
    { -36, MBEDTLS_ECP_DP_SECP521R1, MBEDTLS_MD_SHA512, 64, 133, 132 },  // ES512
};

static const alg_spec_t *alg_lookup(int cose_id) {
    for (size_t i = 0; i < sizeof(ALGS) / sizeof(ALGS[0]); i++) {
        if (ALGS[i].cose_id == cose_id) {
            return &ALGS[i];
        }
    }
    return NULL;
}

// Verify a raw R||S ECDSA signature: 1 = valid, 0 = invalid or malformed. Pure C
// (no MicroPython), so it is exercised directly by the host test. Any unknown alg
// or wrong-width input is rejected before any crypto runs.
int omv_ecdsa_verify(int cose_id,
                     const uint8_t *pub, size_t pub_len,
                     const uint8_t *sig, size_t sig_len,
                     const uint8_t *msg, size_t msg_len) {
    const alg_spec_t *spec = alg_lookup(cose_id);
    if (spec == NULL || pub_len != spec->pub_len || sig_len != spec->sig_len) {
        return 0;
    }

    uint8_t hash[64];   // big enough for SHA-512
    const mbedtls_md_info_t *md = mbedtls_md_info_from_type(spec->md_type);
    size_t half = spec->sig_len / 2;

    mbedtls_ecp_group grp;
    mbedtls_ecp_point Q;
    mbedtls_mpi r, s;
    mbedtls_ecp_group_init(&grp);
    mbedtls_ecp_point_init(&Q);
    mbedtls_mpi_init(&r);
    mbedtls_mpi_init(&s);

    int ok = md != NULL &&
             mbedtls_md(md, msg, msg_len, hash) == 0 &&
             mbedtls_ecp_group_load(&grp, spec->grp_id) == 0 &&
             mbedtls_ecp_point_read_binary(&grp, &Q, pub, pub_len) == 0 &&
             mbedtls_ecp_check_pubkey(&grp, &Q) == 0 &&
             mbedtls_mpi_read_binary(&r, sig, half) == 0 &&
             mbedtls_mpi_read_binary(&s, sig + half, half) == 0 &&
             mbedtls_ecdsa_verify(&grp, hash, spec->hash_len, &Q, &r, &s) == 0;

    mbedtls_mpi_free(&r);
    mbedtls_mpi_free(&s);
    mbedtls_ecp_point_free(&Q);
    mbedtls_ecp_group_free(&grp);
    return ok;
}

// --- signing: the camera's own P-256 key, where it has no secure element ----------------------
//
// A board with no secure element keeps a P-256 private key of its own (openmv_ota.se's software
// identity). These sign with it and derive its public half, on the same mbedtls. This mbedtls has
// no deterministic ECDSA, so the randomness -- the signature's nonce and the scalar blinding --
// comes from the caller: ``entropy`` is bytes from the hardware RNG (os.urandom on the device),
// consumed in order; running out is an error, never a weaker signature.

typedef struct {
    const uint8_t *buf;
    size_t len, pos;
} entropy_t;

static int entropy_rng(void *ctx, unsigned char *out, size_t n) {
    entropy_t *e = (entropy_t *)ctx;
    if (n > e->len - e->pos) {
        return -1;
    }
    for (size_t i = 0; i < n; i++) {
        out[i] = e->buf[e->pos++];
    }
    return 0;
}

// The private scalar d from 32 big-endian bytes, checked to be a valid P-256 key (1 <= d < n).
static int load_key(mbedtls_ecp_group *grp, mbedtls_mpi *d, const uint8_t *priv, size_t priv_len) {
    return priv_len == 32 &&
           mbedtls_ecp_group_load(grp, MBEDTLS_ECP_DP_SECP256R1) == 0 &&
           mbedtls_mpi_read_binary(d, priv, priv_len) == 0 &&
           mbedtls_ecp_check_privkey(grp, d) == 0;
}

// The public key of ``priv``: 65 bytes 04 || X || Y into ``pub``. 1 = done, 0 = bad key/entropy.
int omv_ecdsa_public_key(const uint8_t *priv, size_t priv_len, uint8_t *pub,
                         const uint8_t *entropy, size_t entropy_len) {
    entropy_t e = { entropy, entropy_len, 0 };
    mbedtls_ecp_group grp;
    mbedtls_ecp_point Q;
    mbedtls_mpi d;
    size_t olen = 0;
    mbedtls_ecp_group_init(&grp);
    mbedtls_ecp_point_init(&Q);
    mbedtls_mpi_init(&d);
    int ok = load_key(&grp, &d, priv, priv_len) &&
             mbedtls_ecp_mul(&grp, &Q, &d, &grp.G, entropy_rng, &e) == 0 &&
             mbedtls_ecp_point_write_binary(&grp, &Q, MBEDTLS_ECP_PF_UNCOMPRESSED, &olen,
                                            pub, 65) == 0;
    mbedtls_mpi_free(&d);
    mbedtls_ecp_point_free(&Q);
    mbedtls_ecp_group_free(&grp);
    return ok;
}

// An ECDSA P-256 signature over the 32-byte ``digest``: raw R || S (64 bytes) into ``sig``.
// 1 = signed, 0 = bad key, bad digest length or not enough entropy.
int omv_ecdsa_sign(const uint8_t *priv, size_t priv_len, const uint8_t *digest, size_t digest_len,
                   uint8_t *sig, const uint8_t *entropy, size_t entropy_len) {
    entropy_t e = { entropy, entropy_len, 0 };
    mbedtls_ecp_group grp;
    mbedtls_mpi d, r, s;
    mbedtls_ecp_group_init(&grp);
    mbedtls_mpi_init(&d);
    mbedtls_mpi_init(&r);
    mbedtls_mpi_init(&s);
    int ok = digest_len == 32 &&
             load_key(&grp, &d, priv, priv_len) &&
             mbedtls_ecdsa_sign(&grp, &r, &s, &d, digest, digest_len, entropy_rng, &e) == 0 &&
             mbedtls_mpi_write_binary(&r, sig, 32) == 0 &&
             mbedtls_mpi_write_binary(&s, sig + 32, 32) == 0;
    mbedtls_mpi_free(&s);
    mbedtls_mpi_free(&r);
    mbedtls_mpi_free(&d);
    mbedtls_ecp_group_free(&grp);
    return ok;
}

#ifndef OMV_ECDSA_VERIFY_HOST_TEST   // MicroPython binding (compiled in the firmware)

#include "py/runtime.h"
#include "py/obj.h"

static mp_obj_t mod_ecdsa_verify(size_t n_args, const mp_obj_t *args) {
    (void)n_args;
    mp_buffer_info_t pub, sig, msg;
    mp_get_buffer_raise(args[1], &pub, MP_BUFFER_READ);
    mp_get_buffer_raise(args[2], &sig, MP_BUFFER_READ);
    mp_get_buffer_raise(args[3], &msg, MP_BUFFER_READ);
    int ok = omv_ecdsa_verify((int)mp_obj_get_int(args[0]),
                              (const uint8_t *)pub.buf, pub.len,
                              (const uint8_t *)sig.buf, sig.len,
                              (const uint8_t *)msg.buf, msg.len);
    return ok ? mp_const_true : mp_const_false;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(ecdsa_verify_obj, 4, 4, mod_ecdsa_verify);

// ecdsa_verify.public_key(priv, entropy) -> 65 bytes; sign(priv, digest, entropy) -> 64 bytes R||S.
// ``entropy``: os.urandom bytes, 64 is plenty for either. ValueError on a bad key or input.
static mp_obj_t mod_ecdsa_public_key(mp_obj_t priv_in, mp_obj_t ent_in) {
    mp_buffer_info_t priv, ent;
    mp_get_buffer_raise(priv_in, &priv, MP_BUFFER_READ);
    mp_get_buffer_raise(ent_in, &ent, MP_BUFFER_READ);
    uint8_t pub[65];
    if (!omv_ecdsa_public_key((const uint8_t *)priv.buf, priv.len, pub,
                              (const uint8_t *)ent.buf, ent.len)) {
        mp_raise_ValueError(MP_ERROR_TEXT("bad key or entropy"));
    }
    return mp_obj_new_bytes(pub, sizeof(pub));
}
static MP_DEFINE_CONST_FUN_OBJ_2(ecdsa_public_key_obj, mod_ecdsa_public_key);

static mp_obj_t mod_ecdsa_sign(mp_obj_t priv_in, mp_obj_t digest_in, mp_obj_t ent_in) {
    mp_buffer_info_t priv, digest, ent;
    mp_get_buffer_raise(priv_in, &priv, MP_BUFFER_READ);
    mp_get_buffer_raise(digest_in, &digest, MP_BUFFER_READ);
    mp_get_buffer_raise(ent_in, &ent, MP_BUFFER_READ);
    uint8_t sig[64];
    if (!omv_ecdsa_sign((const uint8_t *)priv.buf, priv.len, (const uint8_t *)digest.buf,
                        digest.len, sig, (const uint8_t *)ent.buf, ent.len)) {
        mp_raise_ValueError(MP_ERROR_TEXT("bad key, digest or entropy"));
    }
    return mp_obj_new_bytes(sig, sizeof(sig));
}
static MP_DEFINE_CONST_FUN_OBJ_3(ecdsa_sign_obj, mod_ecdsa_sign);

static const mp_rom_map_elem_t ecdsa_verify_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__),   MP_ROM_QSTR(MP_QSTR_ecdsa_verify) },
    { MP_ROM_QSTR(MP_QSTR_verify),     MP_ROM_PTR(&ecdsa_verify_obj) },
    { MP_ROM_QSTR(MP_QSTR_public_key), MP_ROM_PTR(&ecdsa_public_key_obj) },
    { MP_ROM_QSTR(MP_QSTR_sign),       MP_ROM_PTR(&ecdsa_sign_obj) },
};
static MP_DEFINE_CONST_DICT(ecdsa_verify_globals, ecdsa_verify_globals_table);

const mp_obj_module_t ecdsa_verify_module = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&ecdsa_verify_globals,
};
MP_REGISTER_MODULE(MP_QSTR_ecdsa_verify, ecdsa_verify_module);

#endif // !OMV_ECDSA_VERIFY_HOST_TEST (MicroPython binding)

#endif // OMV_ECDSA_VERIFY_HOST_TEST || MICROPY_SSL_MBEDTLS

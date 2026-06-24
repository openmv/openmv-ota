// SPDX-License-Identifier: MIT
//
// ecdsa_verify -- ECDSA-over-mbedtls signature verify for the OTA boot.py.
//
// Exposes one function to MicroPython:
//
//   ecdsa_verify.verify(alg, pubkey, sig, msg) -> bool
//     alg    : COSE algorithm id (-7 ES256 / -35 ES384 / -36 ES512)
//     pubkey : uncompressed EC public point, 04 || X || Y
//     sig    : raw R || S signature (fixed width for the curve)
//     msg    : the trailer's signed region; hashed here with the alg's hash
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

static const mp_rom_map_elem_t ecdsa_verify_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_ecdsa_verify) },
    { MP_ROM_QSTR(MP_QSTR_verify),   MP_ROM_PTR(&ecdsa_verify_obj) },
};
static MP_DEFINE_CONST_DICT(ecdsa_verify_globals, ecdsa_verify_globals_table);

const mp_obj_module_t ecdsa_verify_module = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&ecdsa_verify_globals,
};
MP_REGISTER_MODULE(MP_QSTR_ecdsa_verify, ecdsa_verify_module);

#endif // OMV_ECDSA_VERIFY_HOST_TEST

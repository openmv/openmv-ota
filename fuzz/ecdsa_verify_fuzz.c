// libFuzzer target for the device's ECDSA verify shim (src/openmv_ota/build/device/ecdsa_verify.c).
//
// The shim is the one piece of C in the update path that reads attacker-supplied bytes: the
// public key, signature and message of every image a camera is offered. This drives
// omv_ecdsa_verify() with arbitrary keys, signatures and messages under ASan and UBSan, and
// checks the one property that must hold: an answer of 0 or 1, and never a crash, leak or
// undefined behaviour -- whatever the input.
//
// Input: [alg][len mode][pub][sig][msg]. The first byte picks ES256 / ES384 / ES512 or an
// unknown algorithm; the second chooses exact key and signature widths (the interesting case:
// it reaches the curve arithmetic) or arbitrary widths taken from the input (the early
// rejections). Seeds from fuzz/seeds.py are real host-signed vectors, so mutations start
// from inputs that reach mbedtls_ecdsa_verify itself.
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>

extern int omv_ecdsa_verify(int cose_id, const uint8_t *pub, size_t pub_len,
                            const uint8_t *sig, size_t sig_len,
                            const uint8_t *msg, size_t msg_len);

typedef struct { int id; size_t pub; size_t sig; } alg_t;
static const alg_t ALGS[4] = { { -7, 65, 64 }, { -35, 97, 96 }, { -36, 133, 132 }, { 0, 65, 64 } };

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size) {
    if (size < 4) {
        return 0;
    }
    const alg_t *a = &ALGS[data[0] & 3];
    size_t pub_len = a->pub, sig_len = a->sig;
    data += 2;
    size -= 2;
    if (data[-1] & 0x80) {                       // arbitrary widths: the early rejections
        pub_len = data[0] % 160;
        sig_len = data[1] % 160;
        data += 2;
        size -= 2;
    }
    if (pub_len + sig_len > size) {
        return 0;
    }
    // copies, so ASan sees an exact-size allocation for each field (an over-read is caught)
    uint8_t *pub = malloc(pub_len ? pub_len : 1);
    uint8_t *sig = malloc(sig_len ? sig_len : 1);
    size_t msg_len = size - pub_len - sig_len;
    uint8_t *msg = malloc(msg_len ? msg_len : 1);
    for (size_t i = 0; i < pub_len; i++) pub[i] = data[i];
    for (size_t i = 0; i < sig_len; i++) sig[i] = data[pub_len + i];
    for (size_t i = 0; i < msg_len; i++) msg[i] = data[pub_len + sig_len + i];
    int r = omv_ecdsa_verify(a->id, pub, pub_len, sig, sig_len, msg, msg_len);
    if (r != 0 && r != 1) {
        abort();
    }
    free(pub);
    free(sig);
    free(msg);
    return 0;
}

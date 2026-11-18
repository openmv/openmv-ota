/*
 * OpenMV boot stub -- the Alif Ensemble's OSPI decryption engine in software. See ecb_slot.h.
 */
#include <string.h>
#include "ecb_slot.h"

static const uint8_t slot_key_label[6] = { 'o', 'm', 'v', 's', 'l', 't' };

void ecb_slot_key(const uint8_t flash_key[16], const uint8_t tweak[ECB_SLOT_TWEAK_SIZE], uint8_t key[16]) {
    aes128_t a;
    uint8_t block[16];
    memcpy(block, tweak, ECB_SLOT_TWEAK_SIZE);
    memcpy(block + ECB_SLOT_TWEAK_SIZE, slot_key_label, sizeof(slot_key_label));
    aes128_init(&a, flash_key);
    aes128_encrypt(&a, block, key);
    memset(&a, 0, sizeof(a));
}

void ecb_slot_engine_key(const uint8_t key[16], uint8_t out[16]) {
    for (uint32_t i = 0; i < 16; i++) {
        out[i] = key[15 - i];
    }
}

void ecb_slot_init(ecb_slot_t *e, const uint8_t key[16]) {
    aes128_init(&e->aes, key);
}

static void reversed(const uint8_t in[16], uint8_t out[16]) {
    for (uint32_t i = 0; i < 16; i++) {
        out[i] = in[15 - i];
    }
}

void ecb_slot_encrypt(const ecb_slot_t *e, uint8_t *buf, uint32_t n) {
    uint8_t a[16], b[16];
    for (uint32_t off = 0; off + 16 <= n; off += 16) {
        reversed(buf + off, a);
        aes128_encrypt(&e->aes, a, b);
        reversed(b, buf + off);
    }
}

void ecb_slot_decrypt(const ecb_slot_t *e, uint8_t *buf, uint32_t n) {
    uint8_t a[16], b[16];
    for (uint32_t off = 0; off + 16 <= n; off += 16) {
        reversed(buf + off, a);
        aes128_decrypt(&e->aes, a, b);
        reversed(b, buf + off);
    }
}

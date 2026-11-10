#include <string.h>
#include "sha256.h"
#include "slot_tweak.h"

// Changing it changes every slot's keystream: never.
static const uint8_t slot_label[16] = { 'o', 'm', 'v', '-', 's', 'l', 'o', 't', '-', 'c', 'r', 'y', 'p', 't', '-', '1' };

void slot_tweak_derive(uint32_t index, const uint8_t trailer_sha256[32], uint8_t out[SLOT_TWEAK_SIZE]) {
    CRYAL_SHA256_CTX ctx;
    uint8_t hash[32];
    uint8_t idx[4] = { (uint8_t) index, (uint8_t) (index >> 8), (uint8_t) (index >> 16), (uint8_t) (index >> 24) };
    sha256_init(&ctx);
    sha256_update(&ctx, slot_label, sizeof(slot_label));
    sha256_update(&ctx, idx, sizeof(idx));
    sha256_update(&ctx, trailer_sha256, 32);
    sha256_final(&ctx, hash);
    memcpy(out, hash, SLOT_TWEAK_SIZE);
}

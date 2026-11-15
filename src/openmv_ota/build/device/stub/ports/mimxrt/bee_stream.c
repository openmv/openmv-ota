/*
 * OpenMV boot stub -- the i.MX RT1062 BEE counter mode in software. See bee_stream.h.
 */
#include <string.h>
#include "bee_stream.h"

void bee_stream_init(bee_stream_t *b, const uint8_t key[16], const uint8_t tweak[BEE_TWEAK_SIZE]) {
    aes128_init(&b->aes, key);
    memcpy(b->tweak, tweak, BEE_TWEAK_SIZE);
}

void bee_stream_xor(const bee_stream_t *b, uint32_t addr, uint8_t *buf, uint32_t n) {
    uint8_t ctr[16], ks[16];
    memcpy(ctr, b->tweak, BEE_TWEAK_SIZE);
    ctr[10] = 0;
    ctr[11] = 0;
    while (n) {
        uint32_t block = addr >> 4;
        ctr[12] = (uint8_t) (block >> 24);
        ctr[13] = (uint8_t) (block >> 16);
        ctr[14] = (uint8_t) (block >> 8);
        ctr[15] = (uint8_t) block;
        aes128_encrypt(&b->aes, ctr, ks);
        uint32_t off = addr & 15U;
        uint32_t m = 16U - off;
        if (m > n) {
            m = n;
        }
        for (uint32_t i = 0; i < m; i++) {
            buf[i] ^= ks[off + i];
        }
        buf += m;
        addr += m;
        n -= m;
    }
}

// The engine keys with the register bytes reversed: W0's low byte is the key's last.
uint32_t bee_stream_key_word(const uint8_t key[16], unsigned i) {
    const uint8_t *p = key + 12 - 4 * i;
    return ((uint32_t) p[0] << 24) | ((uint32_t) p[1] << 16) | ((uint32_t) p[2] << 8) | p[3];
}

// W3 leads the counter, W0 is zero: the tweak is W3, W2 and the top half of W1.
uint32_t bee_stream_nonce_word(const uint8_t tweak[BEE_TWEAK_SIZE], unsigned i) {
    switch (i) {
        case 3:
            return ((uint32_t) tweak[0] << 24) | ((uint32_t) tweak[1] << 16) | ((uint32_t) tweak[2] << 8) | tweak[3];
        case 2:
            return ((uint32_t) tweak[4] << 24) | ((uint32_t) tweak[5] << 16) | ((uint32_t) tweak[6] << 8) | tweak[7];
        case 1:
            return ((uint32_t) tweak[8] << 24) | ((uint32_t) tweak[9] << 16);
        default:
            return 0;
    }
}

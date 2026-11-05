/*
 * OpenMV boot stub -- the STM32N6 MCE stream cipher in software. See mce_stream.h.
 */
#include <string.h>
#include "mce_stream.h"

static uint32_t be32(const uint8_t *p) {
    return ((uint32_t) p[0] << 24) | ((uint32_t) p[1] << 16) | ((uint32_t) p[2] << 8) | p[3];
}

void mce_stream_init(mce_stream_t *m, const uint8_t key[16], const uint8_t tweak[MCE_TWEAK_SIZE]) {
    aes128_init(&m->aes, key);
    memcpy(m->tweak, tweak, MCE_TWEAK_SIZE);
}

void mce_stream_xor(const mce_stream_t *m, uint32_t addr, uint8_t *buf, uint32_t n) {
    uint8_t ctr[16], ks[16];
    memcpy(ctr, m->tweak, 8);                   // CCNR1 || CCNR0
    ctr[8] = 0;
    ctr[9] = 0;
    ctr[10] = m->tweak[8];                      // VERSION
    ctr[11] = m->tweak[9];
    while (n) {
        uint32_t block = (addr >> 4) & 0x0FFFFFFFU;
        ctr[12] = (uint8_t) (block >> 24);
        ctr[13] = (uint8_t) (block >> 16);
        ctr[14] = (uint8_t) (block >> 8);
        ctr[15] = (uint8_t) block;
        aes128_encrypt(&m->aes, ctr, ks);
        uint32_t off = addr & 15U;
        uint32_t m_n = 16U - off;
        if (m_n > n) {
            m_n = n;
        }
        for (uint32_t i = 0; i < m_n; i++) {
            buf[i] ^= ks[15U - (off + i)];      // the engine's keystream is the block reversed
        }
        buf += m_n;
        addr += m_n;
        n -= m_n;
    }
}

uint32_t mce_stream_key_word(const uint8_t key[16], unsigned i) {
    return be32(key + 4 * (3 - i));             // CCKEYR0 is the key's last word
}

uint32_t mce_stream_nonce_word(const uint8_t tweak[MCE_TWEAK_SIZE], unsigned i) {
    return be32(tweak + 4 * (1 - i));           // CCNR1 leads the counter
}

uint32_t mce_stream_version(const uint8_t tweak[MCE_TWEAK_SIZE]) {
    return ((uint32_t) tweak[8] << 8) | tweak[9];
}

// ST's HAL_MCE_KeyCRCComputation, which the silicon agrees with (0x95 for the key words
// 00010203 04050607 08090a0b 0c0d0e0f on the bench).
uint32_t mce_stream_key_crc(const uint32_t words[4]) {
    static const uint32_t strobe[4] = { 0xAA55AA55U, 0x3U, 0x18U, 0xC0U };
    uint8_t crc = 0;
    for (unsigned j = 0; j < 4; j++) {
        uint32_t v = words[j];
        if (j == 0) {
            v ^= strobe[0];
        } else {
            v ^= (strobe[j] << 24) | ((uint32_t) crc << 16) | (strobe[j] << 8) | crc;
        }
        crc = 0;
        for (unsigned i = 0; i < 32; i++) {
            uint32_t k = (((uint32_t) crc >> 7) ^ ((v >> (31U - i)) & 0xFU)) & 1U;
            crc = (uint8_t) (crc << 1);
            if (k) {
                crc ^= 0x7;
            }
        }
        crc ^= 0x55;
    }
    return crc;
}

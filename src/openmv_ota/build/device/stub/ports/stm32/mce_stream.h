/*
 * OpenMV boot stub -- the STM32N6 memory cipher engine's stream cipher, in software.
 *
 * The MCE decrypts what the core reads through the XSPI memory map; what the stub
 * programs into such a region goes through the flash controller's indirect mode, which
 * the engine is not in. So the stub makes the same keystream itself: this file is the
 * engine's AES-128 stream mode, byte for byte, measured against the silicon (bench,
 * OPENMV_N6, 2026-11-04) and held to those vectors by the host tests:
 *
 *   counter(addr) = CCNR1 || CCNR0 || 0x0000 || VERSION || (addr >> 4 & 0x0FFFFFFF)
 *                   every field big-endian, addr the AXI address (0x70xxxxxx)
 *   keystream     = the bytes of AES-128(key, counter) in REVERSE order
 *   key           = BE(CCKEYR3) || BE(CCKEYR2) || BE(CCKEYR1) || BE(CCKEYR0)
 *
 * The 80-bit tweak is CCNR1, CCNR0 and VERSION in that order. No hardware in here: it
 * builds for the host as it is.
 */
#ifndef STUB_MCE_STREAM_H
#define STUB_MCE_STREAM_H

#include <stdint.h>
#include "aes128.h"

#define MCE_TWEAK_SIZE              (10)

typedef struct {
    aes128_t aes;
    uint8_t tweak[MCE_TWEAK_SIZE];
} mce_stream_t;

void mce_stream_init(mce_stream_t *m, const uint8_t key[16], const uint8_t tweak[MCE_TWEAK_SIZE]);

// XOR the keystream for flash addresses addr..addr+n-1 into buf: encrypts and decrypts.
void mce_stream_xor(const mce_stream_t *m, uint32_t addr, uint8_t *buf, uint32_t n);

// What the engine's registers take for the same key and tweak.
uint32_t mce_stream_key_word(const uint8_t key[16], unsigned i);                    // CCKEYRi
uint32_t mce_stream_nonce_word(const uint8_t tweak[MCE_TWEAK_SIZE], unsigned i);    // CCNRi
uint32_t mce_stream_version(const uint8_t tweak[MCE_TWEAK_SIZE]);                   // CCCFGR.VERSION

// The 8-bit check the engine reports in CCCFGR.KEYCRC once CCKEYR0..3 are written.
uint32_t mce_stream_key_crc(const uint32_t words[4]);

#endif // STUB_MCE_STREAM_H

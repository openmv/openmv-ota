/*
 * OpenMV boot stub -- the i.MX RT1062 bus encryption engine's counter mode, in software.
 *
 * The BEE decrypts what the core reads from an enabled range of the FlexSPI memory map;
 * it does nothing for a write. So what the stub programs into such a range it encrypts
 * itself: this file is the engine's AES-128 counter mode, byte for byte, measured against
 * the silicon (bench, OPENMV_RT1060, 2026-11-13) and held to those vectors by the host tests:
 *
 *   counter(addr) = BE32(NONCE_W3) || BE32(NONCE_W2) || BE32(NONCE_W1) || BE32(addr >> 4)
 *                   addr the AHB address (0x60xxxxxx)
 *   keystream     = AES-128(key, counter), byte i for address (addr & ~15) + i
 *   key           = the bytes written to AES_KEY0_W0..3 (as little-endian words), REVERSED
 *
 * NONCE_W0 stays zero: its bits do not add to the counter, they permute the address field
 * (nibble rotations, byte swaps), and nothing documents how. The 80-bit tweak is the first
 * ten counter bytes, the next two zero. No hardware in here: it builds for the host as it is.
 */
#ifndef STUB_BEE_STREAM_H
#define STUB_BEE_STREAM_H

#include <stdint.h>
#include "aes128.h"

#define BEE_TWEAK_SIZE              (10)

typedef struct {
    aes128_t aes;
    uint8_t tweak[BEE_TWEAK_SIZE];
} bee_stream_t;

void bee_stream_init(bee_stream_t *b, const uint8_t key[16], const uint8_t tweak[BEE_TWEAK_SIZE]);

// XOR the keystream for flash addresses addr..addr+n-1 into buf: encrypts and decrypts.
void bee_stream_xor(const bee_stream_t *b, uint32_t addr, uint8_t *buf, uint32_t n);

// What the engine's registers take for the same key and tweak.
uint32_t bee_stream_key_word(const uint8_t key[16], unsigned i);                    // AES_KEY0_Wi
uint32_t bee_stream_nonce_word(const uint8_t tweak[BEE_TWEAK_SIZE], unsigned i);    // CTR_NONCEn_Wi

#endif // STUB_BEE_STREAM_H

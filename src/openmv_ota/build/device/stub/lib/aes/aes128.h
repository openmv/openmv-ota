/*
 * AES-128, encryption only -- the block function the stub's flash keystream is made of
 * (ports/stm32/mce_stream.c). Small rather than fast: a table of 256 bytes and no
 * precomputed rounds, about 2 KiB of code; the stub runs it over a firmware image once
 * per install, which the flash erases dwarf.
 */
#ifndef STUB_AES128_H
#define STUB_AES128_H

#include <stdint.h>

typedef struct {
    uint8_t rk[176];            // the eleven round keys
} aes128_t;

void aes128_init(aes128_t *a, const uint8_t key[16]);
void aes128_encrypt(const aes128_t *a, const uint8_t in[16], uint8_t out[16]);

#endif // STUB_AES128_H

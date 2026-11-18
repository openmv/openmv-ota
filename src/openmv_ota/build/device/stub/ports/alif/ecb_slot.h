/*
 * OpenMV boot stub -- the Alif Ensemble's OSPI decryption engine, in software, and the
 * camera's flash key.
 *
 * The engine sits on the OSPI controller's memory-mapped (XIP) read path and deciphers
 * every 16-byte block the map hands out, with one key and no address in it: AES-128, one
 * block at a time, the block's bytes reversed going in and coming out. Measured against the
 * silicon (bench, OPENMV_AE3, 2026-11-13) and held to those reads by the host tests:
 *
 *   plaintext block = reverse(AES-128-decrypt(K, reverse(stored block)))
 *   K               = the 16 bytes handed to the Secure Enclave's key service, REVERSED
 *
 * The stub never uses the map: it reads and programs the part through the controller, so
 * it runs this cipher itself, both ways. A block has no position in it, so equal plaintext
 * blocks of one image are equal in flash; what keeps images apart is the key. Each slot
 * image has its own, made from the camera's flash key and the slot's tweak:
 *
 *   K = AES-128-encrypt(flash key, tweak (10 bytes) || "omvslt")
 *
 * The flash key is the camera's own, kept in the chip's MRAM (lib/aes/flash_key.h).
 * No hardware in here: it builds for the host as it is.
 */
#ifndef STUB_ECB_SLOT_H
#define STUB_ECB_SLOT_H

#include <stdbool.h>
#include <stdint.h>
#include "aes128.h"
#include "flash_key.h"

#define ECB_SLOT_TWEAK_SIZE         (10)

typedef struct {
    aes128_t aes;
} ecb_slot_t;

// The key a slot image is ciphered under.
void ecb_slot_key(const uint8_t flash_key[16], const uint8_t tweak[ECB_SLOT_TWEAK_SIZE], uint8_t key[16]);

// What the Secure Enclave's key service takes for the same key.
void ecb_slot_engine_key(const uint8_t key[16], uint8_t out[16]);

void ecb_slot_init(ecb_slot_t *e, const uint8_t key[16]);

// Whole 16-byte blocks, in place: n is a multiple of 16.
void ecb_slot_encrypt(const ecb_slot_t *e, uint8_t *buf, uint32_t n);
void ecb_slot_decrypt(const ecb_slot_t *e, uint8_t *buf, uint32_t n);

#endif // STUB_ECB_SLOT_H

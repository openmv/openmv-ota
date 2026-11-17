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
 * The flash key is sixteen random bytes the firmware makes on the camera's first boot and
 * keeps in the first 256 bytes of the board's key area, in the chip's own MRAM: four
 * 64-byte places, the first complete one being the key. A place is two 32-byte words,
 *
 *   word 0   "OMVF", version 1, zeros           written LAST: it commits the place
 *   word 1   the key, then its bitwise complement
 *
 * so a write a power cut stopped is a place that never counts, and the next one is used.
 * No hardware in here: it builds for the host as it is.
 */
#ifndef STUB_ECB_SLOT_H
#define STUB_ECB_SLOT_H

#include <stdbool.h>
#include <stdint.h>
#include "aes128.h"

#define ECB_SLOT_TWEAK_SIZE         (10)
#define ECB_FLASH_KEY_AREA          (256)       // the bytes of the key area the flash key lives in
#define ECB_FLASH_KEY_PLACE         (64)

typedef struct {
    aes128_t aes;
} ecb_slot_t;

// The camera's flash key out of the first ECB_FLASH_KEY_AREA bytes of its key area: true
// and the key, or false if no place there is complete.
bool ecb_flash_key(const uint8_t *area, uint8_t key[16]);

// The key a slot image is ciphered under.
void ecb_slot_key(const uint8_t flash_key[16], const uint8_t tweak[ECB_SLOT_TWEAK_SIZE], uint8_t key[16]);

// What the Secure Enclave's key service takes for the same key.
void ecb_slot_engine_key(const uint8_t key[16], uint8_t out[16]);

void ecb_slot_init(ecb_slot_t *e, const uint8_t key[16]);

// Whole 16-byte blocks, in place: n is a multiple of 16.
void ecb_slot_encrypt(const ecb_slot_t *e, uint8_t *buf, uint32_t n);
void ecb_slot_decrypt(const ecb_slot_t *e, uint8_t *buf, uint32_t n);

#endif // STUB_ECB_SLOT_H

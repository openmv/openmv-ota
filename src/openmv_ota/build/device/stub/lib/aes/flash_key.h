/*
 * OpenMV boot stub -- the camera's own flash key, on a board whose chip has none to give.
 *
 * Sixteen random bytes the firmware makes on the camera's first boot and keeps in the first
 * 256 bytes of the board's key area, which is inside the chip (the AE3's MRAM, the H7's
 * internal flash): four 64-byte places, the first complete one being the key. A place is
 * two 32-byte words, each programmed once,
 *
 *   word 0   "OMVF", version 1, zeros           written LAST: it commits the place
 *   word 1   the key, then its bitwise complement
 *
 * so a write a power cut stopped is a place that never counts, and the next one is used.
 * No hardware in here: it builds for the host as it is.
 */
#ifndef STUB_FLASH_KEY_H
#define STUB_FLASH_KEY_H

#include <stdbool.h>
#include <stdint.h>

#define FLASH_KEY_AREA              (256)       // the bytes of the key area the flash key lives in
#define FLASH_KEY_PLACE             (64)

// The camera's flash key out of the first FLASH_KEY_AREA bytes of its key area: true and
// the key, or false if no place there is complete.
bool flash_key_read(const uint8_t *area, uint8_t key[16]);

#endif // STUB_FLASH_KEY_H

/*
 * OpenMV boot stub -- the camera's own flash key. See flash_key.h.
 */
#include <string.h>
#include "flash_key.h"

static const uint8_t flash_key_magic[5] = { 'O', 'M', 'V', 'F', 1 };

bool flash_key_read(const uint8_t *area, uint8_t key[16]) {
    for (uint32_t off = 0; off < FLASH_KEY_AREA; off += FLASH_KEY_PLACE) {
        const uint8_t *p = area + off;
        if (memcmp(p, flash_key_magic, sizeof(flash_key_magic)) != 0) {
            continue;
        }
        bool ok = true;
        for (uint32_t i = 0; i < 16; i++) {
            if ((uint8_t) ~p[32 + i] != p[48 + i]) {
                ok = false;
            }
        }
        if (ok) {
            memcpy(key, p + 32, 16);
            return true;
        }
    }
    return false;
}

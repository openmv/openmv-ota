/*
 * OpenMV boot stub -- the i.MX RT1062 bus encryption engine (BEE) and the key it runs on.
 */
#ifndef STUB_BEE_H
#define STUB_BEE_H

#include <stdbool.h>
#include <stdint.h>

int bee_init(void);                                         // the chip's key: 0, or -1
int bee_select(const uint8_t *tweak);                       // the firmware area; NULL: as stored
int bee_slot_select(const uint8_t *tweak, uint32_t start, uint32_t end);
void bee_lock(void);
bool bee_covers(uint32_t addr, uint32_t n);
void bee_xor(uint32_t addr, uint8_t *buf, uint32_t n);      // the ciphered parts, in place
uint32_t bee_pause(void);                                   // every range off: reads are as stored
void bee_resume(uint32_t state);

#endif // STUB_BEE_H

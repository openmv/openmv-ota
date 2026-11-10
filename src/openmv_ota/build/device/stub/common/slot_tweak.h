/*
 * OpenMV boot stub -- the tweak a ROMFS slot's body is ciphered under.
 *
 * On its own here so the host can compile it beside the Python that derives the same
 * value for the installer (tests/build/test_slot_tweak_c.py).
 */
#ifndef STUB_SLOT_TWEAK_H
#define STUB_SLOT_TWEAK_H

#include <stdint.h>

#define SLOT_TWEAK_SIZE     (10)

// 80 bits of SHA-256 over a label, the slot index and the SHA-256 of the slot trailer's
// signed region. The index keeps the two slots apart even when they hold the same release;
// the trailer hash changes with every release, so a slot rewritten with a new image never
// reuses the keystream the old one had at the same addresses. Derived, not stored.
void slot_tweak_derive(uint32_t index, const uint8_t trailer_sha256[32], uint8_t out[SLOT_TWEAK_SIZE]);

#endif // STUB_SLOT_TWEAK_H

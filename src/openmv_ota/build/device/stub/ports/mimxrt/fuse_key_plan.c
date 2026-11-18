/*
 * OpenMV boot stub -- the i.MX RT1062's own key: what to burn next, and whether random words
 * may become a key. Pure C with no chip in it (fuse_key.c does the burning), so the host
 * tests hold it to every case.
 */
#include "fuse_key.h"

int fuse_key_next(uint32_t lock_word) {
    if (!(lock_word & FUSE_KEY_WRITE_LOCK)) {
        return FUSE_KEY_WORDS;                  // the words, then both locks
    }
    return (lock_word & FUSE_KEY_READ_LOCK) ? FUSE_KEY_DONE : FUSE_KEY_READ_LOCK_NEXT;
}

int fuse_key_plausible(const uint32_t words[4]) {
    unsigned ones = 0;
    for (unsigned i = 0; i < 4; i++) {
        if (words[i] == 0 || words[i] == 0xFFFFFFFFU) {
            return 0;
        }
        for (unsigned j = i + 1; j < 4; j++) {
            if (words[i] == words[j]) {
                return 0;
            }
        }
        ones += (unsigned) __builtin_popcount(words[i]);
    }
    return ones >= 32 && ones <= 96;            // 128 fair bits fall outside once in 10^8
}

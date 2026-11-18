/*
 * OpenMV boot stub -- the i.MX RT1062's own key: the SW_GP2 fuses.
 */
#ifndef STUB_FUSE_KEY_H
#define STUB_FUSE_KEY_H

#include <stdint.h>

// The camera's key is in its SW_GP2 fuses, locked against software: 0. Makes it on the boot
// that finds none (random words burnt, then both lock bits). -1: this chip has no key and
// could not be given one.
int fuse_key_ready(void);

// The two SW_GP2 bits of the fuse map's LOCK word: no more programming, no more reading.
#define FUSE_KEY_WRITE_LOCK         (1UL << 21)
#define FUSE_KEY_READ_LOCK          (1UL << 23)

// The order of the work, pure: what fuse_key_ready burns next given the LOCK word.
enum { FUSE_KEY_DONE = 0, FUSE_KEY_WORDS, FUSE_KEY_READ_LOCK_NEXT };
int fuse_key_next(uint32_t lock_word);

// Whether sixteen bytes from the random number generator look like its output: a generator
// that failed quietly gives a run of one value, and that must never become a key.
int fuse_key_plausible(const uint32_t words[4]);

#endif // STUB_FUSE_KEY_H

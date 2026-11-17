/*
 * OpenMV boot stub -- the i.MX RT1062's own key: the SW_GP2 fuses.
 *
 * NXP fuses a master key into every part and gives it to the DCP only once the chip is
 * closed; on an open part the DCP's hardware key is sixteen zero bytes (see bee.c). SW_GP2
 * is the other key the DCP can take: 128 bits of customer fuses with a write lock and a read
 * lock. Once both are burnt software reads 0xBADABADA there and cannot change a bit, and the
 * DCP still gets the real words (measured on the fleet's board, 2026-11-16, open chip: the
 * DCP's output under the key was the same before the read lock, after it, and after a
 * reset). So the camera makes its own key, once: the first boot of this stub on a chip whose
 * SW_GP2 is not locked burns four words from the chip's random number generator and then
 * the two lock bits. Nothing leaves the chip and nothing is stored anywhere else.
 *
 * A FUSE IS FOREVER, so the order is the design. Fuses only go from 0 to 1 and there is no
 * erase. The words are burnt first and the locks last: a power cut in the words leaves an
 * unlocked SW_GP2 with some bits set, and the next boot burns fresh random words on top
 * (still a key nobody knows) and carries on; a cut between the two locks leaves the words
 * final and the next boot burns the read lock. Until both locks are in, bee.c gets no key.
 * Nothing else in the fuse map is written: the two functions that program take only the
 * SW_GP2 words and the two SW_GP2 bits of the LOCK word.
 *
 * The programming sequence and its timing are NXP's (fsl_ocotp.c), by the registers.
 */
#include <string.h>
#include "stub.h"
#include "port.h"
#include "port_mimxrt.h"
#include "fsl_trng.h"
#include "system_MIMXRT1062.h"
#include "fuse_key.h"

#ifdef STUB_CRYPT_START

#define FUSE_LOCK_INDEX             (0x00U)     // the LOCK word
#define FUSE_SW_GP2_INDEX           (0x29U)     // SW_GP2: four words, 0x29..0x2C
#define FUSE_WRITE_LOCK             (FUSE_KEY_WRITE_LOCK)
#define FUSE_READ_LOCK              (FUSE_KEY_READ_LOCK)
_Static_assert(FUSE_KEY_WRITE_LOCK == OCOTP_LOCK_SW_GP2_LOCK_MASK && FUSE_KEY_READ_LOCK == OCOTP_LOCK_SW_GP2_RLOCK_MASK,
               "the SW_GP2 lock bits");
#define OCOTP_UNLOCK_KEY            (0x3E77U)
#define OCOTP_TIMEOUT_MS            (100)
// NXP's figures: the wait, relax, program and read strobes, in nanoseconds.
#define OCOTP_WAIT_NS               (150ULL)
#define OCOTP_RELAX_NS              (100ULL)
#define OCOTP_PROGRAM_NS            (10000ULL)
#define OCOTP_READ_NS               (40ULL)

static int ocotp_idle(void) {
    uint32_t t0 = port_ticks_ms();
    while (OCOTP->CTRL & OCOTP_CTRL_BUSY_MASK) {
        if (port_ticks_ms() - t0 > OCOTP_TIMEOUT_MS) {
            return -1;
        }
    }
    if (OCOTP->CTRL & OCOTP_CTRL_ERROR_MASK) {
        OCOTP->CTRL_CLR = OCOTP_CTRL_ERROR_MASK;
    }
    return 0;
}

// The controller's timing for the clock it runs on (the IPG clock), to read or to program.
// -1 when a figure does not fit its field: a clock this was not worked out for burns nothing.
static int ocotp_timing(bool program) {
    uint64_t hz = SystemCoreClock / (((CCM->CBCDR & CCM_CBCDR_IPG_PODF_MASK) >> CCM_CBCDR_IPG_PODF_SHIFT) + 1U);
    uint64_t wait = (OCOTP_WAIT_NS * hz + 1000000000ULL) / 1000000000ULL - 1U;
    uint64_t relax = (OCOTP_RELAX_NS * hz + 1000000000ULL) / 1000000000ULL - 1U;
    uint64_t strobe_prog = (OCOTP_PROGRAM_NS * hz + 500000000ULL) / 1000000000ULL + 2U * (relax + 1U) - 1U;
    uint64_t strobe_read = (OCOTP_READ_NS * hz + 1000000000ULL) / 1000000000ULL + 2U * (relax + 1U) - 1U;
    if (hz < 1000000U || wait > 0x3FU || relax > 0xFU || strobe_prog > 0xFFFU || strobe_read > 0x3FU) {
        return -1;
    }
    uint32_t t = OCOTP->TIMING & ~(OCOTP_TIMING_RELAX_MASK | OCOTP_TIMING_WAIT_MASK);
    t |= OCOTP_TIMING_RELAX(relax) | OCOTP_TIMING_WAIT(wait);
    if (program) {
        t = (t & ~OCOTP_TIMING_STROBE_PROG_MASK) | OCOTP_TIMING_STROBE_PROG(strobe_prog);
    } else {
        t = (t & ~OCOTP_TIMING_STROBE_READ_MASK) | OCOTP_TIMING_STROBE_READ(strobe_read);
    }
    OCOTP->TIMING = t;
    return 0;
}

// The shadow registers again from the fuses: what was burnt shows, and a lock takes hold.
static int ocotp_reload(void) {
    if (ocotp_idle() != 0 || ocotp_timing(false) != 0 || ocotp_idle() != 0) {
        return -1;
    }
    OCOTP->CTRL_SET = OCOTP_CTRL_RELOAD_SHADOWS_MASK;
    uint32_t t0 = port_ticks_ms();
    while (OCOTP->CTRL & (OCOTP_CTRL_BUSY_MASK | OCOTP_CTRL_RELOAD_SHADOWS_MASK)) {
        if (port_ticks_ms() - t0 > OCOTP_TIMEOUT_MS) {
            return -1;
        }
    }
    return 0;
}

// Burn the 1 bits of `data` into fuse word `index`. Only called by the two functions below.
static int ocotp_program(uint32_t index, uint32_t data) {
    if (ocotp_idle() != 0 || ocotp_timing(true) != 0 || ocotp_idle() != 0) {
        return -1;
    }
    OCOTP->CTRL_CLR = OCOTP_CTRL_ADDR_MASK | OCOTP_CTRL_WR_UNLOCK_MASK;
    OCOTP->CTRL_SET = OCOTP_CTRL_ADDR(index) | OCOTP_CTRL_WR_UNLOCK(OCOTP_UNLOCK_KEY);
    OCOTP->DATA = data;
    uint32_t t0 = port_ticks_ms();
    while (OCOTP->CTRL & OCOTP_CTRL_BUSY_MASK) {
        if (port_ticks_ms() - t0 > OCOTP_TIMEOUT_MS) {
            return -1;
        }
    }
    if (OCOTP->CTRL & OCOTP_CTRL_ERROR_MASK) {
        OCOTP->CTRL_CLR = OCOTP_CTRL_ERROR_MASK;
        return -1;
    }
    return ocotp_reload();
}

// One of the two SW_GP2 lock bits, and nothing else in the LOCK word.
static int burn_lock(uint32_t bit) {
    if (bit != FUSE_WRITE_LOCK && bit != FUSE_READ_LOCK) {
        return -1;
    }
    if (ocotp_program(FUSE_LOCK_INDEX, bit) != 0) {
        return -1;
    }
    return (OCOTP->LOCK & bit) ? 0 : -1;
}

// Sixteen bytes from the chip's random number generator that look like its output.
static int draw_words(uint32_t words[4]) {
    trng_config_t cfg;
    CCM->CCGR6 |= CCM_CCGR6_CG6_MASK;
    (void) TRNG_GetDefaultConfig(&cfg);
    cfg.sampleMode = kTRNG_SampleModeVonNeumann;
    int rc = -1;
    if (TRNG_Init(TRNG, &cfg) == kStatus_Success
        && TRNG_GetRandomData(TRNG, words, 4 * sizeof(uint32_t)) == kStatus_Success
        && fuse_key_plausible(words)) {
        rc = 0;
    }
    TRNG_Deinit(TRNG);
    return rc;
}

// Four words from the random number generator into SW_GP2, each checked while it can still
// be read: every bit asked for is set.
static int burn_words(void) {
    uint32_t words[4];
    int rc = draw_words(words);
    volatile uint32_t *shadow[4] = { &OCOTP->SW_GP20, &OCOTP->SW_GP21, &OCOTP->SW_GP22, &OCOTP->SW_GP23 };
    for (uint32_t i = 0; i < 4 && rc == 0; i++) {
        rc = ocotp_program(FUSE_SW_GP2_INDEX + i, words[i]);
        if (rc == 0 && (*shadow[i] & words[i]) != words[i]) {
            rc = -1;
        }
    }
    memset(words, 0, sizeof(words));
    return rc;
}

#ifdef STUB_FUSE_KEY_SELFTEST
// A bench build (EXTRA_CFLAGS=-DSTUB_FUSE_KEY_SELFTEST), for a chip whose key is already
// burnt: run everything the first boot runs short of programming a fuse -- two draws from
// the random number generator, and the fuse controller's timing and reload on this clock --
// and say how it went. It burns nothing.
static void fuse_key_selftest(void) {
    uint32_t a[4], b[4];
    int rc = draw_words(a);
    if (rc == 0) {
        rc = draw_words(b);
    }
    log_line(rc != 0 ? "fuse key selftest: random draw failed"
             : memcmp(a, b, sizeof(a)) == 0 ? "fuse key selftest: two draws alike" : "fuse key selftest: random ok");
    memset(a, 0, sizeof(a));
    memset(b, 0, sizeof(b));
    log_line(ocotp_timing(true) == 0 && ocotp_reload() == 0 ? "fuse key selftest: timing and reload ok"
             : "fuse key selftest: timing or reload failed");
    log_kv("fuse key selftest: timing", OCOTP->TIMING);
    log_kv("fuse key selftest: lock", OCOTP->LOCK);
}
#endif

int fuse_key_ready(void) {
    CCM->CCGR2 |= CCM_CCGR2_CG6_MASK;           // the fuse controller's clock
    int next = fuse_key_next(OCOTP->LOCK);
    if (next == FUSE_KEY_DONE) {
        #ifdef STUB_FUSE_KEY_SELFTEST
        fuse_key_selftest();
        #endif
        return 0;
    }
    log_line(next == FUSE_KEY_WORDS ? "fuse key: making this camera's key" : "fuse key: finishing the locks");
    if (next == FUSE_KEY_WORDS && (burn_words() != 0 || burn_lock(FUSE_WRITE_LOCK) != 0)) {
        log_line("fuse key: failed");
        return -1;
    }
    if (burn_lock(FUSE_READ_LOCK) != 0 || fuse_key_next(OCOTP->LOCK) != FUSE_KEY_DONE) {
        log_line("fuse key: failed");
        return -1;
    }
    log_line("fuse key: burnt and locked");
    return 0;
}

#endif // STUB_CRYPT_START

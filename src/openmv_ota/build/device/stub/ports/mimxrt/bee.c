/*
 * OpenMV boot stub -- the i.MX RT1062 bus encryption engine (BEE).
 *
 * The BEE sits between the FlexSPI memory map and the core and decrypts what is read from
 * the address ranges IOMUXC_GPR names (four of them; two are used). It has two regions, each
 * with its own key and counter nonce: region 1 is the window REGION1_BOT..TOP, region 0 is
 * everywhere else. The firmware area reads through region 0 and the booted slot through
 * region 1, under one key and two tweaks. Nothing here encrypts: a write goes through the
 * FlexSPI controller, which the engine is not in, so port.c runs the same counter mode in
 * software (bee_stream.c) over what it programs.
 *
 * THE KEY. AES-128-ECB of a fixed label in the DCP under its UNIQUE key, the one derived
 * from the master key NXP fuses into every part (OTPMK). Software cannot read that key, and
 * the same value comes out on every boot, so nothing is stored and nothing is provisioned.
 * But the chip hands the DCP the real master key only once it is CLOSED (HAB): on an open
 * part the unique key is sixteen zero bytes, the same on every board (measured, 2026-11-13:
 * the OTP key, the unique key and a zero payload key encrypt alike). So until a camera is
 * closed its flash is ciphertext under a key anyone can compute -- the data path is the real
 * one, the secrecy is not there yet. Closing the chip changes the key: what was installed
 * no longer reads, and the camera is flashed again over USB, once.
 *
 * Measured against the silicon (bench, OPENMV_RT1060): the counter layout in bee_stream.h;
 * a range's top is its END address (top = end - 8 leaves the last block as stored); and the
 * data cache keeps what it read before a range changed, so port.c drops it on every change.
 */
#include <string.h>
#include "stub.h"
#include "port.h"
#include "port_mimxrt.h"
#include "bee.h"
#include "bee_stream.h"

#ifdef STUB_CRYPT_START

// What the key is derived from. Changing it changes every camera's key: never.
static const uint8_t key_label[16] = { 'o', 'm', 'v', '-', 'f', 'l', 'a', 's', 'h', '-', 'k', 'e', 'y', '-', 'v', '1' };

#define DCP_UNIQUE_KEY              (0xFEU)
#define DCP_TIMEOUT_MS              (100)
#define BEE_KEY_TIMEOUT_MS          (100)
// Security level 3 and counter mode for both regions; the endian swap stays on (bit 7 clear).
#define BEE_CTRL_MODES              (BEE_CTRL_SECURITY_LEVEL_R0(3) | BEE_CTRL_CTRL_AES_MODE_R0(1) | \
                                     BEE_CTRL_SECURITY_LEVEL_R1(3) | BEE_CTRL_CTRL_AES_MODE_R1(1))
#define BEE_RANGE_EN(i)             (1U << (IOMUXC_GPR_GPR11_BEE_DE_RX_EN_SHIFT + (i)))

typedef struct {
    bool active;
    uint32_t start, end;
    bee_stream_t stream;
} bee_area_t;

static bee_area_t areas[2];                     // 0: the firmware area, 1: the booted slot
static uint8_t device_key[16];
static bool have_key = false;

// One AES-128-ECB block under the DCP's unique key. The DCP is a bus master: the packet and
// both buffers go out of the data cache before it runs, and the result is read past it.
static int dcp_unique(const uint8_t in[16], uint8_t out[16]) {
    static uint32_t packet[8] __attribute__((aligned(32)));
    static uint8_t src[32] __attribute__((aligned(32)));
    static uint8_t dst[32] __attribute__((aligned(32)));
    CCM->CCGR0 |= CCM_CCGR0_CG5_MASK;
    DCP->CTRL_CLR = DCP_CTRL_SFTRST_MASK | DCP_CTRL_CLKGATE_MASK;
    // The status register reads zero until the block's clock has run: give the fuse key a
    // moment to show before deciding it never reached the DCP.
    uint32_t t0 = port_ticks_ms();
    while (!(DCP->STAT & DCP_STAT_OTP_KEY_READY_MASK)) {
        if (port_ticks_ms() - t0 > DCP_TIMEOUT_MS) {
            return -1;
        }
    }
    DCP->CHANNELCTRL_SET = 1U;                  // channel 0
    DCP->STAT_CLR = 0xFFU;
    DCP->CH0STAT_CLR = 0xFFU;
    memcpy(src, in, 16);
    memset(dst, 0, sizeof(dst));
    packet[0] = 0;                              // no next packet
    packet[1] = 0x122U | (1U << 10);            // encrypt, cipher on, release the semaphore; an OTP key
    packet[2] = DCP_UNIQUE_KEY << 8;            // ...this one
    packet[3] = (uint32_t) src;
    packet[4] = (uint32_t) dst;
    packet[5] = 16;
    packet[6] = 0;
    packet[7] = 0;
    SCB_CleanDCache_by_Addr((void *) packet, sizeof(packet));
    SCB_CleanDCache_by_Addr((void *) src, sizeof(src));
    SCB_CleanInvalidateDCache_by_Addr((void *) dst, sizeof(dst));
    DCP->CH0CMDPTR = (uint32_t) packet;
    DCP->CH0SEMA = 1U;
    t0 = port_ticks_ms();
    while (DCP->CH0SEMA & DCP_CH0SEMA_VALUE_MASK) {
        if (port_ticks_ms() - t0 > DCP_TIMEOUT_MS) {
            return -1;
        }
    }
    if (DCP->CH0STAT & 0xFFU) {
        return -1;                              // the channel reports an error
    }
    SCB_InvalidateDCache_by_Addr((void *) dst, sizeof(dst));
    memcpy(out, dst, 16);
    memset(dst, 0, sizeof(dst));
    SCB_CleanDCache_by_Addr((void *) dst, sizeof(dst));
    return 0;
}

int bee_init(void) {
    if (dcp_unique(key_label, device_key) != 0) {
        return -1;
    }
    CCM->CCGR4 |= CCM_CCGR4_CG3_MASK;
    IOMUXC_GPR->GPR11 &= ~(BEE_RANGE_EN(0) | BEE_RANGE_EN(1));
    BEE->CTRL = BEE_CTRL_CTRL_SFTRST_N_MASK | BEE_CTRL_CTRL_CLK_EN_MASK;
    BEE->CTRL = (BEE->CTRL & 0xFFFF0037U) | BEE_CTRL_MODES;
    BEE->ADDR_OFFSET0 = 0;
    BEE->ADDR_OFFSET1 = 0;
    BEE->REGION1_BOT = 0;
    BEE->REGION1_TOP = 0;
    have_key = true;
    return 0;
}

// The key for one region, the way the vendor's driver loads it: the key registers are
// shared, KEY_REGION_SEL says whose they are, KEY_VALID latches them.
static int region_key(unsigned region) {
    BEE->CTRL &= ~BEE_CTRL_KEY_VALID_MASK;
    volatile uint32_t *w = &BEE->AES_KEY0_W0;
    for (unsigned i = 0; i < 4; i++) {
        w[i] = bee_stream_key_word(device_key, i);
    }
    BEE->CTRL |= BEE_CTRL_BEE_ENABLE_MASK;
    if (region) {
        BEE->CTRL |= BEE_CTRL_KEY_REGION_SEL_MASK;
    } else {
        BEE->CTRL &= ~BEE_CTRL_KEY_REGION_SEL_MASK;
    }
    BEE->CTRL |= BEE_CTRL_KEY_VALID_MASK;
    uint32_t t0 = port_ticks_ms();
    while (!(BEE->CTRL & BEE_CTRL_KEY_VALID_MASK)) {
        if (port_ticks_ms() - t0 > BEE_KEY_TIMEOUT_MS) {
            return -1;
        }
    }
    return 0;
}

static int area_select(unsigned i, const uint8_t *tweak, uint32_t start, uint32_t end) {
    bee_area_t *a = &areas[i];
    if (!have_key) {
        return (tweak == NULL) ? 0 : -1;
    }
    IOMUXC_GPR->GPR11 &= ~BEE_RANGE_EN(i);      // the range off while its region changes
    a->active = false;
    if (tweak == NULL) {
        return 0;                               // the range as it is stored
    }
    bee_stream_init(&a->stream, device_key, tweak);
    if (region_key(i) != 0) {
        return -1;
    }
    volatile uint32_t *nonce = (i == 0) ? &BEE->CTR_NONCE0_W0 : &BEE->CTR_NONCE1_W0;
    for (unsigned w = 0; w < 4; w++) {
        nonce[w] = bee_stream_nonce_word(tweak, w);
    }
    if (i == 1) {
        BEE->REGION1_BOT = start;               // region 1 is this window; region 0 the rest
        BEE->REGION1_TOP = end;
    }
    volatile uint32_t *range = (i == 0) ? &IOMUXC_GPR->GPR18 : &IOMUXC_GPR->GPR20;
    range[0] = start;
    range[1] = end;                             // the end address: the last block is in
    IOMUXC_GPR->GPR11 |= BEE_RANGE_EN(i);
    a->start = start;
    a->end = end;
    a->active = true;
    return 0;
}

int bee_select(const uint8_t *tweak) {
    return area_select(0, tweak, STUB_CRYPT_START, STUB_CRYPT_END);
}

int bee_slot_select(const uint8_t *tweak, uint32_t start, uint32_t end) {
    return area_select(1, tweak, start, end);
}

// The two ranges can no longer be moved: their registers' lock bits hold until a reset.
// The keys were never readable. (A firmware that switched the engine off would only stop
// itself: what it runs from is ciphertext.)
void bee_lock(void) {
    if (!have_key) {
        return;
    }
    IOMUXC_GPR->GPR18 |= IOMUXC_GPR_GPR18_LOCK_M7_APC_AC_R0_BOT_MASK;
    IOMUXC_GPR->GPR19 |= IOMUXC_GPR_GPR19_LOCK_M7_APC_AC_R0_TOP_MASK;
    IOMUXC_GPR->GPR20 |= IOMUXC_GPR_GPR20_LOCK_M7_APC_AC_R1_BOT_MASK;
    IOMUXC_GPR->GPR21 |= IOMUXC_GPR_GPR21_LOCK_M7_APC_AC_R1_TOP_MASK;
}

static bool area_covers(const bee_area_t *a, uint32_t addr, uint32_t n) {
    return a->active && addr < a->end && a->start < addr + n;
}

bool bee_covers(uint32_t addr, uint32_t n) {
    return area_covers(&areas[0], addr, n) || area_covers(&areas[1], addr, n);
}

void bee_xor(uint32_t addr, uint8_t *buf, uint32_t n) {
    for (unsigned i = 0; i < 2; i++) {
        const bee_area_t *a = &areas[i];
        if (!area_covers(a, addr, n)) {
            continue;
        }
        uint32_t lo = (addr < a->start) ? a->start : addr;
        uint32_t hi = (addr + n > a->end) ? a->end : addr + n;
        bee_stream_xor(&a->stream, lo, buf + (lo - addr), hi - lo);
    }
}

uint32_t bee_pause(void) {
    uint32_t state = IOMUXC_GPR->GPR11 & (BEE_RANGE_EN(0) | BEE_RANGE_EN(1));
    IOMUXC_GPR->GPR11 &= ~state;
    return state;
}

void bee_resume(uint32_t state) {
    IOMUXC_GPR->GPR11 |= state;
}

#endif // STUB_CRYPT_START

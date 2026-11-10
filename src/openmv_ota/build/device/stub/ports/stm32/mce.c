/*
 * OpenMV boot stub -- the STM32N6's firmware area, ciphertext at rest.
 *
 * The N6 keeps its firmware in the external flash it boots from, a part that can be read
 * off the board. So the bytes there are stored encrypted, and the chip's memory cipher
 * engine (MCE2, the one in front of XSPI2) deciphers them as the core reads the memory
 * map. Three things make that work:
 *
 *   the key     Derived on every boot, stored nowhere: AES-256-ECB of a fixed label in
 *               the SAES engine under the DHUK, the key the chip derives from a secret ST
 *               fuses into each part and that no code can read. Nothing is burned and
 *               nothing is provisioned. Until the device is closed and its debug port
 *               locked, code on the chip (or a probe) can derive the same key: this
 *               protects the flash chip, not the running camera.
 *   the tweak   80 bits the caller derives from what is installed (install.c), loaded as
 *               the engine's nonce and version: no two installs share a keystream.
 *   the cipher  The engine's AES-128 stream mode, which mce_stream.c reproduces for what
 *               the stub programs (in indirect mode the engine is not in the path).
 *
 * The engine's registers were worked out on an OPENMV_N6 (2026-11-04), and two things the
 * reference manual does not say are load-bearing here:
 *
 *   - Writing CCCFGR.MODE zeroes the context's key. The key then reads as valid, the
 *     region still ciphers, and the keystream is that of an all-zero key. So the mode is
 *     set BEFORE the key, and the key is checked afterwards (CCCFGR.KEYCRC against the
 *     value computed here) before the context is enabled.
 *   - CCKEYR0..3 must be written in that order; the reverse leaves the key invalid.
 *
 * A standby wake resets the engine. The OpenMV firmware's wake is a full boot (its
 * resume-from-RAM path is linked in flash, where it cannot run), so the stub programs the
 * engine again on the way; a firmware that did resume from retained RAM would have to
 * enter the stub first.
 */
#include <string.h>
#include STM32_HAL_H
#include "stub.h"
#include "port.h"
#include "port_stm32.h"

#if defined(STM32N6) && defined(STUB_CRYPT_START)

#include "stm32n6xx_hal_cryp.h"
#include "stm32n6xx_ll_bus.h"
#include "mce_stream.h"

#define MCE_ENGINE          MCE2
#define MCE_CIPHER_AES128   (1U << MCE_CR_CIPHERSEL_Pos)
#define MCE_KEYCRC(v)       (((v) & MCE_CCCFGR_KEYCRC_Msk) >> MCE_CCCFGR_KEYCRC_Pos)

// What the key is derived from. Changing it changes every camera's key: never.
static const uint8_t key_label[16] = { 'o', 'm', 'v', '-', 'f', 'l', 'a', 's', 'h', '-', 'k', 'e', 'y', '-', 'v', '1' };

// The HAL's CRYP driver records the chip revision at init and never reads it again; the
// function that fetches it is only built into a secure-state HAL, which the stub's is not.
uint32_t HAL_GetREVID(void) {
    return 0;
}

// One ciphered range of the memory map: a region of the engine, the context it ciphers
// under, and the same keystream in software for programming mode. The firmware area has
// region 1 and context 1; the slot the stub boots has region 2 and context 2 (the engine
// has two contexts, which is why the other slot is never mapped).
typedef struct {
    MCE_Region_TypeDef *region;
    MCE_Context_TypeDef *ctx;
    uint32_t ctxid;             // the region's CTXID field for this context
    uint32_t start, end;        // [start, end) while active
    bool active;
    mce_stream_t stream;
} mce_area_t;

static mce_area_t areas[2] = {
    { (MCE_Region_TypeDef *) MCE2_REGION1_BASE, (MCE_Context_TypeDef *) MCE2_CONTEXT1_BASE, MCE_REGCR_CTXID_0 },
    { (MCE_Region_TypeDef *) MCE2_REGION2_BASE, (MCE_Context_TypeDef *) MCE2_CONTEXT2_BASE, MCE_REGCR_CTXID_1 },
};
#define AREA_FIRMWARE       (&areas[0])
#define AREA_SLOT           (&areas[1])

static uint8_t device_key[16];
static bool have_key = false;

int mce_init(void) {
    LL_APB4_GRP2_EnableClock(LL_APB4_GRP2_PERIPH_BSEC);
    if (!(BSEC->SR & BSEC_SR_HVALID)) {
        return -1;                              // SAES would silently use a fixed key instead
    }
    // SAES masks its work with random numbers and waits for them: the RNG must be running,
    // which in the firmware it always is by the time anything asks for a key.
    __HAL_RCC_RNG_CLK_ENABLE();
    SET_BIT(RNG->CR, RNG_CR_RNGEN);
    __HAL_RCC_SAES_CLK_ENABLE();
    uint32_t in[4], out[4];
    memcpy(in, key_label, sizeof(in));
    CRYP_HandleTypeDef h = { 0 };
    h.Instance = SAES;
    h.Init.DataType = CRYP_BYTE_SWAP;
    h.Init.KeySize = CRYP_KEYSIZE_256B;
    h.Init.Algorithm = CRYP_AES_ECB;
    h.Init.DataWidthUnit = CRYP_DATAWIDTHUNIT_BYTE;
    h.Init.KeyIVConfigSkip = CRYP_KEYIVCONFIG_ALWAYS;
    h.Init.KeySelect = CRYP_KEYSEL_HW;          // the DHUK
    h.Init.KeyMode = CRYP_KEYMODE_NORMAL;
    h.Init.KeyProtection = CRYP_KEYPROT_DISABLE;
    bool ok = HAL_CRYP_Init(&h) == HAL_OK &&
              HAL_CRYP_Encrypt(&h, in, sizeof(in), out, 100) == HAL_OK;
    HAL_CRYP_DeInit(&h);
    if (!ok) {
        return -1;
    }
    memcpy(device_key, out, sizeof(device_key));
    memset(out, 0, sizeof(out));
    __HAL_RCC_MCE2_CLK_ENABLE();
    // The cipher, once, before any context is keyed: writing it zeroes every context's key.
    MODIFY_REG(MCE_ENGINE->CR, MCE_CR_CIPHERSEL, MCE_CIPHER_AES128);
    have_key = true;
    return 0;
}

static int area_select(mce_area_t *a, const uint8_t *tweak, uint32_t start, uint32_t end) {
    if (!have_key) {
        return (tweak == NULL) ? 0 : -1;
    }
    a->region->REGCR = 0;                       // the region off while its context changes
    a->ctx->CCCFGR = 0;
    a->active = false;
    if (tweak == NULL) {
        return 0;                               // the range as it is stored
    }
    mce_stream_init(&a->stream, device_key, tweak);
    // The mode before the key: a write to MODE zeroes the key.
    a->ctx->CCCFGR = (mce_stream_version(tweak) << MCE_CCCFGR_VERSION_Pos) | MCE_CCCFGR_MODE_0;
    a->ctx->CCNR0 = mce_stream_nonce_word(tweak, 0);
    a->ctx->CCNR1 = mce_stream_nonce_word(tweak, 1);
    uint32_t words[4];
    for (unsigned i = 0; i < 4; i++) {
        words[i] = mce_stream_key_word(device_key, i);
    }
    a->ctx->CCKEYR0 = words[0];                 // in this order: the reverse is not taken
    a->ctx->CCKEYR1 = words[1];
    a->ctx->CCKEYR2 = words[2];
    a->ctx->CCKEYR3 = words[3];
    bool ok = MCE_KEYCRC(a->ctx->CCCFGR) == mce_stream_key_crc(words);
    memset(words, 0, sizeof(words));
    if (!ok) {
        a->ctx->CCCFGR = 0;
        return -1;                              // the engine holds some other key: never cipher with it
    }
    SET_BIT(a->ctx->CCCFGR, MCE_CCCFGR_CCEN);
    a->region->SADDR = start;
    a->region->EADDR = end - 1U;                // inclusive
    a->region->REGCR = MCE_REGCR_ENC_0 | a->ctxid | MCE_REGCR_BREN;
    a->start = start;
    a->end = end;
    a->active = true;
    return 0;
}

int mce_select(const uint8_t *tweak) {
    return area_select(AREA_FIRMWARE, tweak, STUB_CRYPT_START, STUB_CRYPT_END);
}

int mce_slot_select(const uint8_t *tweak, uint32_t start, uint32_t end) {
    return area_select(AREA_SLOT, tweak, start, end);
}

void mce_lock(void) {
    if (!have_key) {
        return;
    }
    // Whatever the firmware runs can no longer move a region, change a key or switch
    // the cipher off: the registers stay as they are until the next reset.
    for (unsigned i = 0; i < 2; i++) {
        SET_BIT(areas[i].ctx->CCCFGR, MCE_CCCFGR_KEYLOCK | MCE_CCCFGR_CCLOCK);
    }
    SET_BIT(MCE_ENGINE->CR, MCE_CR_GLOCK);
}

static bool area_covers(const mce_area_t *a, uint32_t addr, uint32_t n) {
    return a->active && addr < a->end && a->start < addr + n;
}

bool mce_covers(uint32_t addr, uint32_t n) {
    return area_covers(AREA_FIRMWARE, addr, n) || area_covers(AREA_SLOT, addr, n);
}

// The parts of [addr, addr + n) that lie in a ciphered range, ciphered in place.
void mce_xor(uint32_t addr, uint8_t *buf, uint32_t n) {
    for (unsigned i = 0; i < 2; i++) {
        const mce_area_t *a = &areas[i];
        if (!area_covers(a, addr, n)) {
            continue;
        }
        uint32_t lo = (addr < a->start) ? a->start : addr;
        uint32_t hi = (addr + n > a->end) ? a->end : addr + n;
        mce_stream_xor(&a->stream, lo, buf + (lo - addr), hi - lo);
    }
}

#endif // STM32N6 && STUB_CRYPT_START

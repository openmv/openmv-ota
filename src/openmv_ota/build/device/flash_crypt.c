// SPDX-License-Identifier: MIT
//
// flash_crypt -- the slot cipher for the OTA installer, on a board whose boot stub keeps
// the ROMFS slots ciphertext at rest (the STM32N6).
//
// Exposes to MicroPython:
//
//   flash_crypt.xor(addr, buf, tweak) -> None
//     addr  : absolute flash address (the memory-mapped view) of buf[0]
//     buf   : a writable buffer, ciphered in place: plaintext in, ciphertext out, and back
//     tweak : the slot's 10-byte tweak (openmv_ota derives it from the manifest)
//
// The stub installs the firmware and boots one slot through the chip's memory cipher
// engine; the installer writes the OTHER slot, which the engine does not cover, so it
// applies the same keystream itself: the engine's AES-128 stream mode, reproduced in
// software exactly as the stub does for what it programs (mce_stream.c, the file above
// this one in the generated module). The key is the stub's: AES-256-ECB of a fixed label
// in the SAES under the DHUK, derived here on first use and kept for the process. It is
// never handed to Python.
//
// `openmv-ota build firmware` writes this file into the firmware's modules/ dir, with the
// stub's aes128 and mce_stream sources pasted above it, for a board whose stub layout is
// encrypted; and removes it after the build.
//
// RAM BUDGET: ciphers the caller's buffer in place, 16 bytes of keystream at a time.

#include <stddef.h>
#include <stdint.h>
#include <string.h>

#if defined(OMV_FLASH_CRYPT_HOST_TEST)
#include "aes128.h"
#include "mce_stream.h"
#else
#include "py/mphal.h"                            // the port's HAL: defines its family
#endif

#if defined(OMV_FLASH_CRYPT_HOST_TEST) || defined(STM32N6)

#define FLASH_CRYPT_TWEAK_SIZE  (MCE_TWEAK_SIZE)

// XOR the keystream for flash addresses addr.. into buf: 0, or -13 (EACCES: the chip has no
// hardware key -- SAES would silently use a fixed one instead) or -5 (EIO: SAES failed).
// Pure C over the key the port hands it; host-tested with a known key.
int omv_flash_crypt_xor(const uint8_t key[16], const uint8_t tweak[FLASH_CRYPT_TWEAK_SIZE],
                               uint32_t addr, uint8_t *buf, size_t len) {
    mce_stream_t m;
    mce_stream_init(&m, key, tweak);
    mce_stream_xor(&m, addr, buf, (uint32_t) len);
    memset(&m, 0, sizeof(m));
    return 0;
}

#if !defined(OMV_FLASH_CRYPT_HOST_TEST)

// What the key is derived from: the stub's label (ports/stm32/mce.c). Never changes.
static const uint8_t flash_crypt_label[16] = { 'o', 'm', 'v', '-', 'f', 'l', 'a', 's', 'h', '-', 'k', 'e', 'y', '-', 'v', '1' };

// The SAES by its registers (the HAL's CRYP driver is not built into the firmware): one
// AES-256-ECB block of the label under the DHUK, byte-swapped data, as the stub's HAL call
// configures it. -13 when the chip's hardware key is not valid, -5 on a timeout.
static int omv_flash_crypt_derive(uint8_t key[16]) {
    if (!(BSEC->SR & BSEC_SR_HVALID)) {
        return -13;
    }
    __HAL_RCC_RNG_CLK_ENABLE();                  // SAES masks its work with the RNG
    SET_BIT(RNG->CR, RNG_CR_RNGEN);
    __HAL_RCC_SAES_CLK_ENABLE();
    SET_BIT(SAES->CR, SAES_CR_IPRST);
    (void) READ_BIT(SAES->CR, SAES_CR_IPRST);
    CLEAR_BIT(SAES->CR, SAES_CR_IPRST);
    uint32_t t0 = HAL_GetTick();
    while (SAES->SR & SAES_SR_BUSY) {
        if (HAL_GetTick() - t0 > 100) {
            return -5;
        }
    }
    // The DHUK, normal key mode, no key protection, 8-bit data (byte swap), 256-bit key,
    // ECB, encrypt.
    MODIFY_REG(SAES->CR,
               SAES_CR_KEYSEL | SAES_CR_KMOD | SAES_CR_KEYPROT | SAES_CR_DATATYPE | SAES_CR_KEYSIZE |
               SAES_CR_CHMOD | SAES_CR_MODE,
               SAES_CR_KEYSEL_0 | (2U << SAES_CR_DATATYPE_Pos) | SAES_CR_KEYSIZE);
    t0 = HAL_GetTick();
    while (!(SAES->SR & SAES_SR_KEYVALID)) {
        if (HAL_GetTick() - t0 > 100) {
            return -5;
        }
    }
    SET_BIT(SAES->CR, SAES_CR_EN);
    uint32_t in[4], out[4];
    memcpy(in, flash_crypt_label, sizeof(in));
    for (unsigned i = 0; i < 4; i++) {
        SAES->DINR = in[i];
    }
    t0 = HAL_GetTick();
    while (!(SAES->SR & SAES_SR_CCF)) {
        if (HAL_GetTick() - t0 > 100) {
            CLEAR_BIT(SAES->CR, SAES_CR_EN);
            return -5;
        }
    }
    SET_BIT(SAES->ICR, SAES_ICR_CCF);
    for (unsigned i = 0; i < 4; i++) {
        out[i] = SAES->DOUTR;
    }
    CLEAR_BIT(SAES->CR, SAES_CR_EN);
    memcpy(key, out, 16);
    memset(out, 0, sizeof(out));
    return 0;
}

#include "py/runtime.h"
#include "py/obj.h"

static uint8_t flash_crypt_key[16];
static bool flash_crypt_have_key = false;

static mp_obj_t mod_flash_crypt_xor(mp_obj_t addr_in, mp_obj_t buf_in, mp_obj_t tweak_in) {
    mp_buffer_info_t buf, tweak;
    mp_get_buffer_raise(buf_in, &buf, MP_BUFFER_RW);
    mp_get_buffer_raise(tweak_in, &tweak, MP_BUFFER_READ);
    if (tweak.len != FLASH_CRYPT_TWEAK_SIZE) {
        mp_raise_ValueError(MP_ERROR_TEXT("tweak must be 10 bytes"));
    }
    if (!flash_crypt_have_key) {
        int r = omv_flash_crypt_derive(flash_crypt_key);
        if (r != 0) {
            mp_raise_OSError(-r);
        }
        flash_crypt_have_key = true;
    }
    uint32_t addr = mp_obj_get_int_truncated(addr_in);
    omv_flash_crypt_xor(flash_crypt_key, (const uint8_t *)tweak.buf, addr, (uint8_t *)buf.buf, buf.len);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_3(flash_crypt_xor_obj, mod_flash_crypt_xor);

static const mp_rom_map_elem_t flash_crypt_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_flash_crypt) },
    { MP_ROM_QSTR(MP_QSTR_xor),      MP_ROM_PTR(&flash_crypt_xor_obj) },
};
static MP_DEFINE_CONST_DICT(flash_crypt_globals, flash_crypt_globals_table);

const mp_obj_module_t flash_crypt_module = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&flash_crypt_globals,
};
MP_REGISTER_MODULE(MP_QSTR_flash_crypt, flash_crypt_module);

#endif // !OMV_FLASH_CRYPT_HOST_TEST (MicroPython binding)

#endif // OMV_FLASH_CRYPT_HOST_TEST || STM32N6

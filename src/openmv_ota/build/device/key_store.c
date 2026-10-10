// SPDX-License-Identifier: MIT
//
// key_store -- write-once storage for the camera's own keys, on a board with no secure element.
//
// The keys live at the very end of the boot partition: flash the bootloader never reaches (the
// build refuses a bootloader that would) and that nothing else writes, so updating the firmware
// or the romfs never touches them, and a later lockdown's read protection covers them where they
// already are. This only reads the area and programs blank flash in it -- there is no erase
// here, so it can't take the bootloader with it. What the bytes mean is the Python side's
// business (openmv_ota.se.soft).
//
// Exposes to MicroPython:
//
//   key_store.SIZE, key_store.BLANK  the key area's size in bytes, and what a blank byte reads
//   key_store.read(offset, length)   ``length`` bytes at ``offset`` in the key area (inside it,
//                                    else ValueError) -- the caller reads it a slot at a time.
//                                    OSError(EIO) if a flash word there can't be read (below)
//   key_store.seal / unseal          N6 only: AES-256-GCM under the chip's own key (see below)
//   key_store.write(offset, data)    program ``data`` at ``offset`` in it. Both a multiple of
//                                    32 bytes (one flash word on every backend), inside the area,
//                                    and every byte there still blank -- else OSError (EINVAL,
//                                    EEXIST) and nothing written. EIO if the flash refused, or
//                                    if a flash word there can't be read.
//
// A flash word that can't be read: the H7's flash keeps ECC per 256-bit word, and a power cut
// while one is being programmed can leave it with an error ECC can't correct. Reading it answers
// with a bus fault, so a plain read would crash the board -- at every boot, since the keys are
// read at boot. Here it reads as EIO instead (see omv_key_store_copy).
//
// The area's address is board data: `openmv-ota build firmware` puts this file in the firmware's
// modules/ dir with ``OMV_KEY_AREA_ADDR`` defined at the top (boards.json, ``secure_element``),
// and only for a board that keeps its keys here. Each port's way of programming its flash is one
// ``#if`` branch below; the checks are pure C, host-tested (OMV_KEY_STORE_HOST_TEST).

#include <stddef.h>
#include <stdint.h>
#include <string.h>

#ifndef OMV_KEY_AREA_ADDR
#error "key_store: OMV_KEY_AREA_ADDR not defined (openmv-ota build firmware defines it)"
#endif

#define KEY_AREA_SIZE   (4096)
#define KEY_WRITE_UNIT  (32)

#if defined(OMV_KEY_STORE_HOST_TEST)

extern uint8_t omv_key_store_host_area[];       // the host test's stand-in for the flash
int omv_key_store_program(uintptr_t addr, const uint8_t *src, size_t len);
int omv_key_store_copy(uint8_t *dst, uintptr_t addr, size_t len);

#else
#include "py/mphal.h"                            // the port's HAL: defines its family (STM32H7, ...)
#endif

// The AE3's helper core (M55_HE) is slaved to the main core and never touches the keys: on it
// this whole unit is empty, so there is no module, and no second core that could program MRAM.
#if !defined(CORE_M55_HE)

#if defined(CORE_M55_HP)
#define KEY_BLANK       (0x00)                   // MRAM: no erased state; erasing writes zeros
#else
#define KEY_BLANK       (0xFF)                   // flash: what an erase leaves
#endif

#if defined(OMV_KEY_STORE_HOST_TEST)
#elif defined(STM32F4) || defined(STM32F7) || defined(STM32H7)

#include "flash.h"

// Every pending flash error flag, as MicroPython's flash.c defines it per family (F4's HAL has
// no such macro; F7's does; H7's has one per bank).
#if defined(STM32F4)
#define KEY_FLASH_ERRORS (FLASH_FLAG_EOP | FLASH_FLAG_OPERR | FLASH_FLAG_WRPERR | \
                          FLASH_FLAG_PGAERR | FLASH_FLAG_PGPERR | FLASH_FLAG_PGSERR)
#elif defined(STM32H7)
#define KEY_FLASH_ERRORS (FLASH_FLAG_ALL_ERRORS_BANK1 | FLASH_FLAG_ALL_ERRORS_BANK2)
#else
#define KEY_FLASH_ERRORS FLASH_FLAG_ALL_ERRORS
#endif

// MicroPython's own internal-flash programmer: a word at a time on F4/F7, a 256-bit flash word
// at a time on H7 (KEY_WRITE_UNIT keeps every write whole and aligned for both).
static int omv_key_store_program(uintptr_t addr, const uint8_t *src, size_t len) {
    uint32_t words[KEY_WRITE_UNIT / 4];
    // flash_write() doesn't clear error flags an earlier operation left (flash_erase() does), and
    // the HAL refuses to program while one is pending: measured on an F427, the first write after
    // `flash factory` failed EIO with the area blank, and succeeded once the flags were clear.
    __HAL_FLASH_CLEAR_FLAG(KEY_FLASH_ERRORS);
    for (size_t off = 0; off < len; off += KEY_WRITE_UNIT) {
        memcpy(words, src + off, KEY_WRITE_UNIT);
        if (flash_write(addr + off, words, KEY_WRITE_UNIT / 4) != 0) {
            return -1;
        }
    }
    #if defined(__DCACHE_PRESENT) && __DCACHE_PRESENT
    // The blank check below read these lines through the D-cache (F7/H7); drop them so the
    // next read sees what was just programmed.
    SCB_InvalidateDCache_by_Addr((void *)addr, (int32_t)len);
    #endif
    return 0;
}

#if defined(STM32H7)

// The key area is the end of the bootloader's sector, always in bank 1.
static int omv_key_store_ecc_failed(uintptr_t lo, int32_t span) {
    if (!__HAL_FLASH_GET_FLAG(FLASH_FLAG_DBECCERR_BANK1)) {
        return 0;
    }
    // Only a failure in what was just read: the M7 also fetches flash speculatively, so a bad
    // word nearby could raise the flag during a read of good ones (seen on a Pure Thermal: the
    // flag was back, set by nothing this code read, after a read had cleared it).
    uintptr_t at = FLASH_BANK1_BASE + (FLASH->ECC_FA1 & FLASH_ECC_FA_FAIL_ECC_ADDR) * 32;
    __HAL_FLASH_CLEAR_FLAG(FLASH_FLAG_DBECCERR_BANK1);
    return at >= lo && at < lo + (uintptr_t)span;
}

// Copy ``len`` bytes of the key area at ``addr``: 0, or -1 if a flash word there failed its ECC
// (a double error: a power cut while it was programmed; reproduced on a Pure Thermal by
// programming one flash word twice). The read runs with bus faults ignored
// -- FAULTMASK raises the CPU to priority -1, where CCR.BFHFNMIGN applies -- and the flash
// controller's double-error flag says afterwards whether any of it was bad. The D-cache lines are
// dropped first so the bytes come from the flash, not from a line cached before it went bad.
static int omv_key_store_copy(uint8_t *dst, uintptr_t addr, size_t len) {
    uintptr_t lo = addr & ~(uintptr_t)31;
    int32_t span = (int32_t)(((addr + len + 31) & ~(uintptr_t)31) - lo);
    SCB_InvalidateDCache_by_Addr((void *)lo, span);
    __HAL_FLASH_CLEAR_FLAG(FLASH_FLAG_DBECCERR_BANK1);
    uint32_t ccr = SCB->CCR;
    __disable_fault_irq();
    SCB->CCR = ccr | SCB_CCR_BFHFNMIGN_Msk;
    __DSB();
    __ISB();
    for (size_t i = 0; i < len; i++) {
        dst[i] = ((const volatile uint8_t *)addr)[i];
    }
    __DSB();
    SCB->CCR = ccr;
    __ISB();
    __enable_fault_irq();
    if (omv_key_store_ecc_failed(lo, span)) {
        SCB_InvalidateDCache_by_Addr((void *)lo, span);   // don't keep what the bad word gave
        return -1;
    }
    return 0;
}

#else

// F4/F7: no ECC on the flash, so nothing it holds fails a read.
static int omv_key_store_copy(uint8_t *dst, uintptr_t addr, size_t len) {
    memcpy(dst, (const void *)addr, len);
    return 0;
}

#endif

#elif defined(CORE_M55_HP)

#include "irq.h"
#include "mpu.h"
#include "mram.h"

// The AE3 keeps its keys in MRAM, programmed 16 bytes at a time by Alif's mram_write_128bit,
// each with interrupts OFF: an interrupt taken while MRAM programs wedges its controller until a
// power cycle (Alif's Driver_MRAM.c requires them off; openmv_ota._write_masked does the same).
// MRAM has no erased state -- the Secure Enclave's MRAM erase writes zeros, and so does
// `openmv-ota flash bootloader` over this area -- so blank here is 0x00. The MPU maps MRAM
// write-through, so the cache never holds a stale copy of what was just programmed.
static int omv_key_store_program(uintptr_t addr, const uint8_t *src, size_t len) {
    mpu_config_mram(false);
    for (size_t off = 0; off < len; off += 16) {
        uint32_t irq = disable_irq();
        mram_write_128bit((uint8_t *)(addr + off), src + off);
        enable_irq(irq);
    }
    mpu_config_mram(true);
    return 0;
}

static int omv_key_store_copy(uint8_t *dst, uintptr_t addr, size_t len) {
    memcpy(dst, (const void *)addr, len);
    return 0;
}

#elif defined(STM32N6)

// The N6 has no internal flash: the key area is in the external NOR it boots from, read through
// the XSPI's memory map and written with MicroPython's own XSPI flash driver (the one the romfs
// writer uses), which leaves memory-mapped mode for the program and returns to it after.
#include "xspi.h"
#include "storage.h"

static int omv_key_store_program(uintptr_t addr, const uint8_t *src, size_t len) {
    uint32_t off = addr - xspi_get_xip_base(&xspi_flash2);
    int r = spi_bdev_writeblocks_raw(MICROPY_HW_ROMFS_XSPI_SPIBDEV_OBJ, src, 0, off, len);
    SCB_InvalidateDCache_by_Addr((void *)addr, (int32_t)len);   // mapped reads see it now
    return r;
}

// The keys are SEALED with the chip's own key: AES-256-GCM in SAES, keyed by the DHUK -- derived
// in hardware from ST's per-chip secret, never readable -- so the NOR alone, or on another chip,
// opens nothing. The AES-GCM is ST's (its HAL driver, built here with the module switched on for
// this file only); this only sets it up. The DHUK depends on the HDPL and the security state,
// so it is always used from the firmware as it runs today: secure, privileged, HDPL 1.
#define HAL_CRYP_MODULE_ENABLED
#include "stm32n6xx_hal_cryp.h"
#include "../lib/stm32/n6/src/stm32n6xx_hal_cryp.c"     // modules/ -> the firmware tree
#include "../lib/stm32/n6/src/stm32n6xx_hal_cryp_ex.c"

#define KEY_SEAL_MAX    (KEY_WRITE_UNIT * 5)     // at most the five key words of a record

static void omv_be_words(uint32_t *w, const uint8_t *b, size_t n) {
    for (size_t i = 0; i < n / 4; i++) {
        w[i] = ((uint32_t)b[4 * i] << 24) | ((uint32_t)b[4 * i + 1] << 16) |
               ((uint32_t)b[4 * i + 2] << 8) | b[4 * i + 3];
    }
}

// GCM over ``len`` bytes of ``in`` into ``out`` with ``aad`` (one 32-byte word) authenticated,
// under the DHUK; the 16-byte tag into ``tag``. 0, or -13 (EACCES: the chip's hardware key is
// not valid -- SAES would silently use a fixed key instead) or -5 (EIO: SAES failed).
static int omv_key_store_gcm(int encrypt, const uint8_t *aad, const uint8_t *iv,
                             const uint8_t *in, size_t len, uint8_t *out, uint8_t *tag) {
    if (!(BSEC->SR & BSEC_SR_HVALID)) {
        return -13;
    }
    __HAL_RCC_RNG_CLK_ENABLE();                  // SAES masks with the RNG
    __HAL_RCC_SAES_CLK_ENABLE();
    uint32_t ivw[4], aadw[KEY_WRITE_UNIT / 4], inw[KEY_SEAL_MAX / 4], outw[KEY_SEAL_MAX / 4];
    uint32_t tagw[4];
    omv_be_words(ivw, iv, 12);
    ivw[3] = 2;                                  // GCM: J0 = IV || 0x00000001, counting from 2
    memcpy(aadw, aad, KEY_WRITE_UNIT);
    memcpy(inw, in, len);
    CRYP_HandleTypeDef h = { 0 };
    h.Instance = SAES;
    h.Init.DataType = CRYP_BYTE_SWAP;
    h.Init.KeySize = CRYP_KEYSIZE_256B;
    h.Init.Algorithm = CRYP_AES_GCM;
    h.Init.pInitVect = ivw;
    h.Init.Header = aadw;
    h.Init.HeaderSize = KEY_WRITE_UNIT;
    h.Init.HeaderWidthUnit = CRYP_HEADERWIDTHUNIT_BYTE;
    h.Init.DataWidthUnit = CRYP_DATAWIDTHUNIT_BYTE;
    h.Init.KeyIVConfigSkip = CRYP_KEYIVCONFIG_ALWAYS;
    h.Init.KeySelect = CRYP_KEYSEL_HW;           // the DHUK
    h.Init.KeyMode = CRYP_KEYMODE_NORMAL;
    h.Init.KeyProtection = CRYP_KEYPROT_DISABLE;
    int ok = HAL_CRYP_Init(&h) == HAL_OK &&
             (encrypt ? HAL_CRYP_Encrypt(&h, inw, len, outw, 100)
                      : HAL_CRYP_Decrypt(&h, inw, len, outw, 100)) == HAL_OK &&
             HAL_CRYPEx_AESGCM_GenerateAuthTAG(&h, tagw, 100) == HAL_OK;
    HAL_CRYP_DeInit(&h);
    memcpy(out, outw, len);
    memcpy(tag, tagw, 16);
    memset(inw, 0, sizeof(inw));                 // the plain keys don't linger on the stack
    memset(outw, 0, sizeof(outw));
    return ok ? 0 : -5;
}

// NOR flash with no ECC of its own: a read always succeeds.
static int omv_key_store_copy(uint8_t *dst, uintptr_t addr, size_t len) {
    memcpy(dst, (const void *)addr, len);
    return 0;
}

#else
#error "key_store: no flash backend for this port"
#endif

// Copy ``len`` bytes at ``offset`` in the key area into ``dst``: 0, or -22 (EINVAL: outside the
// area) or -5 (EIO: a flash word there can't be read).
int omv_key_store_read(size_t offset, uint8_t *dst, size_t len) {
    if (offset > KEY_AREA_SIZE || len > KEY_AREA_SIZE - offset) {
        return -22;
    }
    return omv_key_store_copy(dst, OMV_KEY_AREA_ADDR + offset, len) == 0 ? 0 : -5;
}

// Program ``len`` bytes of ``src`` at ``offset`` in the key area: 0, or -22 (EINVAL: not whole
// write units, or outside the area), -17 (EEXIST: not blank there) or -5 (EIO: flash refused).
int omv_key_store_write(size_t offset, const uint8_t *src, size_t len) {
    if (len == 0 || offset % KEY_WRITE_UNIT || len % KEY_WRITE_UNIT ||
        offset > KEY_AREA_SIZE || len > KEY_AREA_SIZE - offset) {
        return -22;
    }
    uint8_t there[KEY_WRITE_UNIT];
    for (size_t at = offset; at < offset + len; at += KEY_WRITE_UNIT) {
        if (omv_key_store_copy(there, OMV_KEY_AREA_ADDR + at, KEY_WRITE_UNIT) != 0) {
            return -5;
        }
        for (size_t i = 0; i < KEY_WRITE_UNIT; i++) {
            if (there[i] != KEY_BLANK) {
                return -17;
            }
        }
    }
    return omv_key_store_program(OMV_KEY_AREA_ADDR + offset, src, len) == 0 ? 0 : -5;
}

#ifndef OMV_KEY_STORE_HOST_TEST   // MicroPython binding (compiled in the firmware)

#include "py/runtime.h"
#include "py/obj.h"

static mp_obj_t mod_key_store_write(mp_obj_t offset_in, mp_obj_t data_in) {
    mp_buffer_info_t data;
    mp_get_buffer_raise(data_in, &data, MP_BUFFER_READ);
    int r = omv_key_store_write((size_t)mp_obj_get_int(offset_in), (const uint8_t *)data.buf,
                                data.len);
    if (r != 0) {
        mp_raise_OSError(-r);
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_2(key_store_write_obj, mod_key_store_write);

static mp_obj_t mod_key_store_read(mp_obj_t offset_in, mp_obj_t length_in) {
    mp_int_t offset = mp_obj_get_int(offset_in);
    mp_int_t length = mp_obj_get_int(length_in);
    if (offset < 0 || length < 0 || offset > KEY_AREA_SIZE || length > KEY_AREA_SIZE - offset) {
        mp_raise_ValueError(MP_ERROR_TEXT("outside the key area"));
    }
    vstr_t vstr;
    vstr_init_len(&vstr, length);
    int r = omv_key_store_read((size_t)offset, (uint8_t *)vstr.buf, (size_t)length);
    if (r != 0) {
        vstr_clear(&vstr);
        mp_raise_OSError(-r);
    }
    return mp_obj_new_bytes_from_vstr(&vstr);
}
static MP_DEFINE_CONST_FUN_OBJ_2(key_store_read_obj, mod_key_store_read);

#if defined(STM32N6)
// key_store.seal(aad, iv, plain) -> ciphertext || tag; key_store.unseal(aad, iv, ct, tag) ->
// plain. ``aad``: the record's 32-byte description; ``iv``: 12 bytes; at most five key words.
// OSError EACCES if the chip's hardware key isn't valid, or (unseal) the tag doesn't match.
static mp_obj_t mod_key_store_gcm(size_t n_args, const mp_obj_t *args) {
    int encrypt = n_args == 3;
    mp_buffer_info_t aad, iv, in, tag;
    mp_get_buffer_raise(args[0], &aad, MP_BUFFER_READ);
    mp_get_buffer_raise(args[1], &iv, MP_BUFFER_READ);
    mp_get_buffer_raise(args[2], &in, MP_BUFFER_READ);
    if (aad.len != KEY_WRITE_UNIT || iv.len != 12 || in.len == 0 || in.len > KEY_SEAL_MAX ||
        in.len % KEY_WRITE_UNIT) {
        mp_raise_ValueError(MP_ERROR_TEXT("bad seal input"));
    }
    uint8_t out[KEY_SEAL_MAX + 16], calc[16];
    int r = omv_key_store_gcm(encrypt, aad.buf, iv.buf, in.buf, in.len, out, calc);
    if (r == 0 && !encrypt) {
        mp_get_buffer_raise(args[3], &tag, MP_BUFFER_READ);
        uint8_t diff = tag.len != 16;            // constant time over the 16 bytes
        for (size_t i = 0; i < 16; i++) {
            diff |= calc[i] ^ (i < tag.len ? ((const uint8_t *)tag.buf)[i] : 0);
        }
        if (diff) {
            memset(out, 0, sizeof(out));
            r = -13;
        }
    }
    if (r != 0) {
        mp_raise_OSError(-r);
    }
    if (encrypt) {
        memcpy(out + in.len, calc, 16);
    }
    mp_obj_t o = mp_obj_new_bytes(out, in.len + (encrypt ? 16 : 0));
    memset(out, 0, sizeof(out));
    return o;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(key_store_seal_obj, 3, 3, mod_key_store_gcm);
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(key_store_unseal_obj, 4, 4, mod_key_store_gcm);
#endif

static const mp_rom_map_elem_t key_store_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_key_store) },
    { MP_ROM_QSTR(MP_QSTR_SIZE),     MP_ROM_INT(KEY_AREA_SIZE) },
    { MP_ROM_QSTR(MP_QSTR_BLANK),    MP_ROM_INT(KEY_BLANK) },
    { MP_ROM_QSTR(MP_QSTR_read),     MP_ROM_PTR(&key_store_read_obj) },
    { MP_ROM_QSTR(MP_QSTR_write),    MP_ROM_PTR(&key_store_write_obj) },
    #if defined(STM32N6)
    { MP_ROM_QSTR(MP_QSTR_seal),     MP_ROM_PTR(&key_store_seal_obj) },
    { MP_ROM_QSTR(MP_QSTR_unseal),   MP_ROM_PTR(&key_store_unseal_obj) },
    #endif
};
static MP_DEFINE_CONST_DICT(key_store_globals, key_store_globals_table);

const mp_obj_module_t key_store_module = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&key_store_globals,
};
MP_REGISTER_MODULE(MP_QSTR_key_store, key_store_module);

#endif // !OMV_KEY_STORE_HOST_TEST

#endif // not the AE3's helper core

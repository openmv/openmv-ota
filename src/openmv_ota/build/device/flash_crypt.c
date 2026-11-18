// SPDX-License-Identifier: MIT
//
// flash_crypt -- the slot cipher for the OTA installer, on a board whose boot stub keeps
// the ROMFS slots ciphertext at rest (the STM32N6, the i.MX RT1062; the Alif Ensemble's
// module, which also has to switch the chip's engine on, is the second half of this file).
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
// applies the same keystream itself: the engine's AES-128 counter mode, reproduced in
// software exactly as the stub does for what it programs (mce_stream.c on the N6,
// bee_stream.c on the RT1062: the file above this one in the generated module). The key is
// the stub's, derived here on first use and kept for the process: on the N6, AES-256-ECB
// of a fixed label in the SAES under the DHUK; on the RT1062, AES-128-ECB of the same
// label in the DCP under the key in the SW_GP2 fuses. It is never handed to Python.
//
// `openmv-ota build firmware` writes this file into the firmware's modules/ dir, with the
// stub's aes128 and stream sources pasted above it, for a board whose stub layout is
// encrypted; and removes it after the build.
//
// The STM32H743 boards (the OpenMV Cam H7 Plus, the Pure Thermal) have no engine at all:
// their module is the same cipher under a key the camera made for itself, plus `load`,
// which deciphers the booted slot into RAM for boot.py to mount (see FLASH_CRYPT_SOFT).
//
// RAM BUDGET: ciphers the caller's buffer in place, 16 bytes of keystream at a time. `load`
// takes one block of the frame-buffer pool as big as the booted image, once per boot.

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
#define FLASH_CRYPT_MCE         (1)
#elif defined(BEE) && defined(DCP)
#define FLASH_CRYPT_BEE         (1)
#elif defined(CORE_M55_HP)
#define FLASH_CRYPT_ECB         (1)                 // the Alif Ensemble: see the end of this file
#elif defined(STM32H7)
#define FLASH_CRYPT_SOFT        (1)                 // no engine: the booted slot is read into RAM
#endif

#if defined(FLASH_CRYPT_MCE) || defined(FLASH_CRYPT_BEE) || defined(FLASH_CRYPT_SOFT)

#define FLASH_CRYPT_TWEAK_SIZE  (10)

// XOR the keystream for flash addresses addr.. into buf: 0, or -13 (EACCES: the chip has no
// hardware key -- SAES would silently use a fixed one instead) or -5 (EIO: SAES failed).
// Pure C over the key the port hands it; host-tested with a known key.
int omv_flash_crypt_xor(const uint8_t key[16], const uint8_t tweak[FLASH_CRYPT_TWEAK_SIZE],
                               uint32_t addr, uint8_t *buf, size_t len) {
    #if defined(FLASH_CRYPT_MCE) || defined(FLASH_CRYPT_SOFT)
    mce_stream_t m;
    mce_stream_init(&m, key, tweak);
    mce_stream_xor(&m, addr, buf, (uint32_t) len);
    #else
    bee_stream_t m;
    bee_stream_init(&m, key, tweak);
    bee_stream_xor(&m, addr, buf, (uint32_t) len);
    #endif
    memset(&m, 0, sizeof(m));
    return 0;
}

#if !defined(OMV_FLASH_CRYPT_HOST_TEST)

#if !defined(FLASH_CRYPT_SOFT)
// What the key is derived from: the stub's label (mce.c, bee.c). Never changes.
static const uint8_t flash_crypt_label[16] = { 'o', 'm', 'v', '-', 'f', 'l', 'a', 's', 'h', '-', 'k', 'e', 'y', '-', 'v', '1' };
#endif

#if defined(FLASH_CRYPT_SOFT)

// The camera's own key, out of the key area in the chip's internal flash (the stub's
// flash_key.h): -13 until the firmware has made it (boot.py does, on the first boot). The
// area is read the way key_store reads it, so a flash word a power cut tore is "no key"
// and not a bus fault.
#ifndef OMV_KEY_AREA_ADDR
#error "flash_crypt: OMV_KEY_AREA_ADDR not defined (openmv-ota build firmware defines it)"
#endif
int omv_key_store_copy(uint8_t *dst, uintptr_t addr, size_t len);    // key_store.c

static int omv_flash_crypt_derive(uint8_t key[16]) {
    uint8_t area[FLASH_KEY_AREA];
    int r = (omv_key_store_copy(area, OMV_KEY_AREA_ADDR, sizeof(area)) == 0 && flash_key_read(area, key)) ? 0 : -13;
    memset(area, 0, sizeof(area));
    return r;
}

#elif defined(FLASH_CRYPT_BEE)

// The DCP by its registers: one AES-128-ECB block of the label under the key in the chip's
// SW_GP2 fuses, as the stub's bee.c runs it -- the DCP's key input pointed at the fuses, the
// block reset so it takes them. -13 when the fuse key has not reached the DCP, or when what
// comes out is the key an all-zero hardware key gives (an open chip off the fuses: never a
// camera's key); -5 on a timeout or a channel error.
static int omv_flash_crypt_derive(uint8_t key[16]) {
    static uint32_t packet[8] __attribute__((aligned(32)));
    static uint8_t src[32] __attribute__((aligned(32)));
    static uint8_t dst[32] __attribute__((aligned(32)));
    CCM->CCGR0 |= CCM_CCGR0_CG5_MASK;
    IOMUXC_GPR->GPR10 |= IOMUXC_GPR_GPR10_DCPKEY_OCOTP_OR_KEYMUX_MASK;
    if (!(IOMUXC_GPR->GPR10 & IOMUXC_GPR_GPR10_DCPKEY_OCOTP_OR_KEYMUX_MASK)) {
        return -13;                              // the mux is locked the other way until a reset
    }
    DCP->CTRL_SET = DCP_CTRL_SFTRST_MASK;
    uint32_t t0 = mp_hal_ticks_ms();
    while (mp_hal_ticks_ms() - t0 < 2) {
    }
    DCP->CTRL_CLR = DCP_CTRL_SFTRST_MASK | DCP_CTRL_CLKGATE_MASK;
    t0 = mp_hal_ticks_ms();                      // the status reads zero until the clock has run
    while (!(DCP->STAT & DCP_STAT_OTP_KEY_READY_MASK)) {
        if (mp_hal_ticks_ms() - t0 > 100) {
            return -13;
        }
    }
    DCP->CHANNELCTRL_SET = 1U;
    DCP->STAT_CLR = 0xFFU;
    DCP->CH0STAT_CLR = 0xFFU;
    memcpy(src, flash_crypt_label, 16);
    memset(dst, 0, sizeof(dst));
    packet[0] = 0;
    packet[1] = 0x122U | (1U << 10);             // encrypt, cipher on, release the semaphore; an OTP key
    packet[2] = 0xFFU << 8;                      // ...the one on its hardware input: SW_GP2
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
    t0 = mp_hal_ticks_ms();
    while (DCP->CH0SEMA & DCP_CH0SEMA_VALUE_MASK) {
        if (mp_hal_ticks_ms() - t0 > 100) {
            return -5;
        }
    }
    if (DCP->CH0STAT & 0xFFU) {
        return -5;
    }
    SCB_InvalidateDCache_by_Addr((void *) dst, sizeof(dst));
    memcpy(key, dst, 16);
    memset(dst, 0, sizeof(dst));
    static const uint8_t zero[16];
    aes128_t a;
    uint8_t open_key[16];
    aes128_init(&a, zero);
    aes128_encrypt(&a, flash_crypt_label, open_key);
    if (memcmp(open_key, key, 16) == 0) {
        memset(key, 0, 16);
        return -13;
    }
    return 0;
}

#else

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

#endif // FLASH_CRYPT_BEE

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

#if defined(FLASH_CRYPT_SOFT)

// ---- No engine (the OpenMV Cam H7 Plus, the Pure Thermal) -------------------------------
//
// The STM32H743 cannot decipher its QSPI part as it reads it, so the firmware reads the
// booted slot ONCE, at boot, into RAM, and /rom is mounted there:
//
//   flash_crypt.keyed() -> bool               does the camera have its flash key?
//   flash_crypt.load(addr, size, tweak) -> memoryview
//                                             size bytes of the slot image with this tweak,
//                                             stored at the address addr, deciphered into
//                                             the module's one RAM copy: the view is of
//                                             that copy. A second call reuses the memory.
//   flash_crypt.loaded() -> memoryview        the copy the last load made, or None
//   flash_crypt.forget() -> None              boot.py calls this first: a soft reset has
//                                             given the copy's memory back
//
// The copy comes from the firmware's frame-buffer pool (uma_malloc, persistent: a collect
// leaves it), which every soft reset starts over, and it is as big as the image, not the
// slot. The flash key (above) is kept for the process and never handed to Python.
#include "py/mperrno.h"
#include "umalloc.h"

#define FLASH_CRYPT_COPY_MAX    (16UL << 20)        // no slot is bigger

static uint8_t *flash_crypt_copy = NULL;
static size_t flash_crypt_copy_room = 0;            // bytes allocated
static size_t flash_crypt_copy_len = 0;             // bytes the last load filled

static mp_obj_t mod_flash_crypt_keyed(void) {
    uint8_t key[16];
    bool keyed = flash_crypt_have_key || omv_flash_crypt_derive(key) == 0;
    memset(key, 0, sizeof(key));
    return mp_obj_new_bool(keyed);
}
static MP_DEFINE_CONST_FUN_OBJ_0(flash_crypt_keyed_obj, mod_flash_crypt_keyed);

static mp_obj_t mod_flash_crypt_forget(void) {
    flash_crypt_copy = NULL;
    flash_crypt_copy_room = 0;
    flash_crypt_copy_len = 0;
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(flash_crypt_forget_obj, mod_flash_crypt_forget);

static mp_obj_t mod_flash_crypt_loaded(void) {
    if (flash_crypt_copy == NULL || flash_crypt_copy_len == 0) {
        return mp_const_none;
    }
    return mp_obj_new_memoryview('B', flash_crypt_copy_len, flash_crypt_copy);
}
static MP_DEFINE_CONST_FUN_OBJ_0(flash_crypt_loaded_obj, mod_flash_crypt_loaded);

static mp_obj_t mod_flash_crypt_load(mp_obj_t addr_in, mp_obj_t size_in, mp_obj_t tweak_in) {
    mp_buffer_info_t tweak;
    mp_get_buffer_raise(tweak_in, &tweak, MP_BUFFER_READ);
    if (tweak.len != FLASH_CRYPT_TWEAK_SIZE) {
        mp_raise_ValueError(MP_ERROR_TEXT("tweak must be 10 bytes"));
    }
    uint32_t addr = mp_obj_get_int_truncated(addr_in);
    size_t size = (size_t)mp_obj_get_int(size_in);
    if (size == 0 || size > FLASH_CRYPT_COPY_MAX) {
        mp_raise_ValueError(MP_ERROR_TEXT("bad size"));
    }
    if (!flash_crypt_have_key) {
        int r = omv_flash_crypt_derive(flash_crypt_key);
        if (r != 0) {
            mp_raise_OSError(-r);
        }
        flash_crypt_have_key = true;
    }
    flash_crypt_copy_len = 0;
    if (flash_crypt_copy == NULL || flash_crypt_copy_room < size) {
        if (flash_crypt_copy != NULL) {
            uma_free(flash_crypt_copy);
            flash_crypt_copy = NULL;
            flash_crypt_copy_room = 0;
        }
        size_t room = (size + 31) & ~(size_t)31;
        flash_crypt_copy = uma_malloc(room, UMA_PERSIST | UMA_MAYBE);
        if (flash_crypt_copy == NULL) {
            mp_raise_OSError(MP_ENOMEM);
        }
        flash_crypt_copy_room = room;
    }
    memcpy(flash_crypt_copy, (const void *)addr, size);
    omv_flash_crypt_xor(flash_crypt_key, (const uint8_t *)tweak.buf, addr, flash_crypt_copy, size);
    flash_crypt_copy_len = size;
    return mp_obj_new_memoryview('B', size, flash_crypt_copy);
}
static MP_DEFINE_CONST_FUN_OBJ_3(flash_crypt_load_obj, mod_flash_crypt_load);

#endif // FLASH_CRYPT_SOFT

static const mp_rom_map_elem_t flash_crypt_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_flash_crypt) },
    { MP_ROM_QSTR(MP_QSTR_xor),      MP_ROM_PTR(&flash_crypt_xor_obj) },
    #if defined(FLASH_CRYPT_SOFT)
    { MP_ROM_QSTR(MP_QSTR_keyed),    MP_ROM_PTR(&flash_crypt_keyed_obj) },
    { MP_ROM_QSTR(MP_QSTR_load),     MP_ROM_PTR(&flash_crypt_load_obj) },
    { MP_ROM_QSTR(MP_QSTR_loaded),   MP_ROM_PTR(&flash_crypt_loaded_obj) },
    { MP_ROM_QSTR(MP_QSTR_forget),   MP_ROM_PTR(&flash_crypt_forget_obj) },
    #endif
};
static MP_DEFINE_CONST_DICT(flash_crypt_globals, flash_crypt_globals_table);

const mp_obj_module_t flash_crypt_module = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&flash_crypt_globals,
};
MP_REGISTER_MODULE(MP_QSTR_flash_crypt, flash_crypt_module);

#endif // !OMV_FLASH_CRYPT_HOST_TEST (MicroPython binding)

#endif // FLASH_CRYPT_MCE || FLASH_CRYPT_BEE || FLASH_CRYPT_SOFT

#if defined(FLASH_CRYPT_ECB)

// ---- The Alif Ensemble (the OpenMV AE3) -------------------------------------------------
//
// Exposes to MicroPython:
//
//   flash_crypt.keyed() -> bool            does the camera have its flash key?
//   flash_crypt.enabled() -> bool          is the engine deciphering, under a key this
//                                          module gave it?
//   flash_crypt.disable() -> None          switch the engine off. A reset that restarts
//                                          the firmware does not always reset the engine
//                                          (measured: the reset pin leaves it on, keyed):
//                                          boot.py calls this before it reads anything.
//   flash_crypt.enable(tweak) -> None      key the engine for the slot image with this
//                                          tweak and switch it on. The engine takes a key
//                                          only while it is off.
//   flash_crypt.raw(addr, buf) -> None     fill buf with the bytes stored at the address
//                                          addr (the memory-mapped view), whatever the
//                                          engine is doing
//   flash_crypt.encrypt(addr, buf, tweak)  cipher buf in place, whole 16-byte blocks, as
//                                          the slot image with this tweak is stored
//
// The chip's engine sits on the OSPI part's memory map and, once on, deciphers EVERY
// block read through it under ONE key -- the booted slot's body, which is what it is for,
// and also everything else on the part: the other slot, each slot's status sector and
// trailer, and /flash, none of which is stored under that key. The engine cannot be
// switched off for those reads (a bus master reading the slot meanwhile -- the NPU, on a
// model mapped in place -- would get ciphertext), so they are undone instead: what the
// map returns is run back through the cipher in software, which gives the stored bytes
// exactly. `raw` does that for Python, and `ospi_flash_read`, the one function /flash and
// the USB drive read through, is wrapped at link time to do it for them
// (-Wl,--wrap=ospi_flash_read, added by `openmv-ota build firmware`).
//
// The key is the camera's own (ecb_slot.h): made by boot.py on the first boot, kept in
// the key area in MRAM, read here and never handed to Python. `openmv-ota build firmware`
// writes this file with OMV_KEY_AREA_ADDR defined at its top and the stub's aes128 and
// ecb_slot sources pasted above it.

#ifndef OMV_KEY_AREA_ADDR
#error "flash_crypt: OMV_KEY_AREA_ADDR not defined (openmv-ota build firmware defines it)"
#endif

#include "py/runtime.h"
#include "py/obj.h"
#include "py/mperrno.h"
#include "services_lib_api.h"
#include "peripheral_types.h"
#include "global_map.h"

#define FLASH_CRYPT_TWEAK_SIZE  (ECB_SLOT_TWEAK_SIZE)
#define FLASH_CRYPT_SE_HANDLE   (0)                 // se_services.c: MHU_SESS_MHU0, channel 0
#define FLASH_CRYPT_AES         ((AES_Type *) AES0_BASE)
#define FLASH_CRYPT_XIP_BASE    (OSPI0_XIP_BASE)
#define FLASH_CRYPT_XIP_SIZE    (OSPI0_XIP_SIZE)
#define FLASH_CRYPT_PART_SIZE   (0x02000000UL)      // the 32 MiB part behind the map

// Not cleared by a soft reset: the engine stays on across one, and so does what it is keyed with.
// A reset that clears these and leaves the engine on is what flash_crypt.disable() is for.
static ecb_slot_t flash_crypt_boot;                 // the engine's key: what `raw` undoes
static bool flash_crypt_on = false;
static ecb_slot_t flash_crypt_slot;                 // the last key `encrypt` was asked for
static uint8_t flash_crypt_slot_tweak[FLASH_CRYPT_TWEAK_SIZE];
static bool flash_crypt_slot_set = false;

static bool flash_crypt_engine_on(void) {
    return (FLASH_CRYPT_AES->AES_CONTROL & AES_CONTROL_DECRYPT_EN) != 0;
}

// The stored bytes of [addr, addr + len) of the map into dst: a plain copy with the engine
// off, and with it on, what the map returns ciphered back. Blocks are 16 bytes of the part.
static void flash_crypt_copy_stored(uint32_t addr, uint8_t *dst, size_t len) {
    if (!flash_crypt_on || !flash_crypt_engine_on()) {
        memcpy(dst, (const void *)addr, len);
        return;
    }
    // With the engine on, the map keeps answering a block it has just read from what it
    // read then, even after the part was programmed there; a read somewhere else on the
    // part makes it fetch again (measured: a mark written and read back came back blank
    // until another address was read). So one is made first, 1 MiB away, past the cache.
    uint32_t far = FLASH_CRYPT_XIP_BASE + (((addr - FLASH_CRYPT_XIP_BASE) + 0x100000) % FLASH_CRYPT_PART_SIZE);
    far &= ~(uint32_t)31;
    SCB_InvalidateDCache_by_Addr((void *)far, 32);
    (void)*(volatile const uint32_t *)far;
    while (len) {
        uint32_t off = addr % 16;
        size_t n;
        if (off != 0 || len < 16) {
            uint8_t block[16];
            memcpy(block, (const void *)(addr - off), 16);
            ecb_slot_encrypt(&flash_crypt_boot, block, 16);
            n = (16 - off < len) ? 16 - off : len;
            memcpy(dst, block + off, n);
        } else {
            n = len & ~(size_t)15;
            memcpy(dst, (const void *)addr, n);
            ecb_slot_encrypt(&flash_crypt_boot, dst, (uint32_t)n);
        }
        addr += n;
        dst += n;
        len -= n;
    }
}

// Off, and nothing of what was read through it left in the caches.
static void flash_crypt_switch_off(void) {
    mp_uint_t state = MICROPY_BEGIN_ATOMIC_SECTION();
    FLASH_CRYPT_AES->AES_CONTROL &= ~AES_CONTROL_DECRYPT_EN;
    flash_crypt_on = false;
    SCB_CleanInvalidateDCache();
    SCB_InvalidateICache();
    MICROPY_END_ATOMIC_SECTION(state);
}

// /flash and the USB drive: MicroPython's ospi_flash_read(addr, len, dest) copies out of the
// map, addr relative to the part.
int __real_ospi_flash_read(uint32_t addr, uint32_t len, uint8_t *dest);
int __wrap_ospi_flash_read(uint32_t addr, uint32_t len, uint8_t *dest) {
    if (!flash_crypt_on) {
        // On, and not by this module: left on by the boot before a reset that did not reset
        // it. /flash is mounted before boot.py runs, so this is where it is first met.
        if (flash_crypt_engine_on()) {
            flash_crypt_switch_off();
        }
        return __real_ospi_flash_read(addr, len, dest);
    }
    flash_crypt_copy_stored(FLASH_CRYPT_XIP_BASE + addr, dest, len);
    return 0;
}

static mp_obj_t mod_flash_crypt_keyed(void) {
    uint8_t key[16];
    bool keyed = flash_key_read((const uint8_t *)OMV_KEY_AREA_ADDR, key);
    memset(key, 0, sizeof(key));
    return mp_obj_new_bool(keyed);
}
static MP_DEFINE_CONST_FUN_OBJ_0(flash_crypt_keyed_obj, mod_flash_crypt_keyed);

static mp_obj_t mod_flash_crypt_enabled(void) {
    return mp_obj_new_bool(flash_crypt_on && flash_crypt_engine_on());
}
static MP_DEFINE_CONST_FUN_OBJ_0(flash_crypt_enabled_obj, mod_flash_crypt_enabled);

static void flash_crypt_tweak_key(mp_obj_t tweak_in, uint8_t key[16]) {
    mp_buffer_info_t tweak;
    mp_get_buffer_raise(tweak_in, &tweak, MP_BUFFER_READ);
    if (tweak.len != FLASH_CRYPT_TWEAK_SIZE) {
        mp_raise_ValueError(MP_ERROR_TEXT("tweak must be 10 bytes"));
    }
    uint8_t flash_key[16];
    if (!flash_key_read((const uint8_t *)OMV_KEY_AREA_ADDR, flash_key)) {
        mp_raise_OSError(MP_EACCES);                // the camera has no flash key
    }
    ecb_slot_key(flash_key, (const uint8_t *)tweak.buf, key);
    memset(flash_key, 0, sizeof(flash_key));
}

static mp_obj_t mod_flash_crypt_disable(void) {
    if (flash_crypt_on || flash_crypt_engine_on()) {
        flash_crypt_switch_off();
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(flash_crypt_disable_obj, mod_flash_crypt_disable);

static mp_obj_t mod_flash_crypt_enable(mp_obj_t tweak_in) {
    uint8_t key[16], engine_key[16];
    flash_crypt_tweak_key(tweak_in, key);
    if (flash_crypt_on || flash_crypt_engine_on()) {
        flash_crypt_switch_off();                   // it takes a key only while it is off
    }
    ecb_slot_engine_key(key, engine_key);
    uint32_t err = 0xFFFFFFFF;
    uint32_t ret = SERVICES_application_ospi_write_key(FLASH_CRYPT_SE_HANDLE, OSPI_WRITE_EXTERNAL_KEY_OSPI0,
                                                       engine_key, &err);
    memset(engine_key, 0, sizeof(engine_key));
    if (ret != 0 || err != OSPI_WRITE_KEY_SUCCESS) {
        memset(key, 0, sizeof(key));
        mp_raise_OSError(MP_EIO);
    }
    ecb_slot_init(&flash_crypt_boot, key);
    memset(key, 0, sizeof(key));
    // Nothing may read the part between the switch and the caches being emptied of what
    // they hold of it from before.
    mp_uint_t state = MICROPY_BEGIN_ATOMIC_SECTION();
    FLASH_CRYPT_AES->AES_CONTROL |= AES_CONTROL_LD_KEY | AES_CONTROL_DECRYPT_EN;
    flash_crypt_on = true;
    SCB_CleanInvalidateDCache();
    SCB_InvalidateICache();
    MICROPY_END_ATOMIC_SECTION(state);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(flash_crypt_enable_obj, mod_flash_crypt_enable);

static mp_obj_t mod_flash_crypt_raw(mp_obj_t addr_in, mp_obj_t buf_in) {
    mp_buffer_info_t buf;
    mp_get_buffer_raise(buf_in, &buf, MP_BUFFER_WRITE);
    uint32_t addr = mp_obj_get_int_truncated(addr_in);
    if (addr < FLASH_CRYPT_XIP_BASE || buf.len > FLASH_CRYPT_XIP_SIZE
        || addr - FLASH_CRYPT_XIP_BASE > FLASH_CRYPT_XIP_SIZE - buf.len) {
        memcpy(buf.buf, (const void *)addr, buf.len);   // not the part (MRAM): nothing to undo
        return mp_const_none;
    }
    flash_crypt_copy_stored(addr, (uint8_t *)buf.buf, buf.len);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_2(flash_crypt_raw_obj, mod_flash_crypt_raw);

static mp_obj_t mod_flash_crypt_encrypt(mp_obj_t addr_in, mp_obj_t buf_in, mp_obj_t tweak_in) {
    mp_buffer_info_t buf, tweak;
    mp_get_buffer_raise(buf_in, &buf, MP_BUFFER_RW);
    mp_get_buffer_raise(tweak_in, &tweak, MP_BUFFER_READ);
    if ((mp_obj_get_int_truncated(addr_in) % 16) != 0 || (buf.len % 16) != 0) {
        mp_raise_ValueError(MP_ERROR_TEXT("whole 16-byte blocks only"));
    }
    if (!flash_crypt_slot_set || tweak.len != FLASH_CRYPT_TWEAK_SIZE
        || memcmp(tweak.buf, flash_crypt_slot_tweak, FLASH_CRYPT_TWEAK_SIZE) != 0) {
        uint8_t key[16];
        flash_crypt_slot_set = false;
        flash_crypt_tweak_key(tweak_in, key);
        ecb_slot_init(&flash_crypt_slot, key);
        memset(key, 0, sizeof(key));
        memcpy(flash_crypt_slot_tweak, tweak.buf, FLASH_CRYPT_TWEAK_SIZE);
        flash_crypt_slot_set = true;
    }
    ecb_slot_encrypt(&flash_crypt_slot, (uint8_t *)buf.buf, (uint32_t)buf.len);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_3(flash_crypt_encrypt_obj, mod_flash_crypt_encrypt);

static const mp_rom_map_elem_t flash_crypt_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_flash_crypt) },
    { MP_ROM_QSTR(MP_QSTR_keyed),    MP_ROM_PTR(&flash_crypt_keyed_obj) },
    { MP_ROM_QSTR(MP_QSTR_enabled),  MP_ROM_PTR(&flash_crypt_enabled_obj) },
    { MP_ROM_QSTR(MP_QSTR_enable),   MP_ROM_PTR(&flash_crypt_enable_obj) },
    { MP_ROM_QSTR(MP_QSTR_disable),  MP_ROM_PTR(&flash_crypt_disable_obj) },
    { MP_ROM_QSTR(MP_QSTR_raw),      MP_ROM_PTR(&flash_crypt_raw_obj) },
    { MP_ROM_QSTR(MP_QSTR_encrypt),  MP_ROM_PTR(&flash_crypt_encrypt_obj) },
};
static MP_DEFINE_CONST_DICT(flash_crypt_globals, flash_crypt_globals_table);

const mp_obj_module_t flash_crypt_module = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&flash_crypt_globals,
};
MP_REGISTER_MODULE(MP_QSTR_flash_crypt, flash_crypt_module);

#endif // FLASH_CRYPT_ECB

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
//   key_store.SIZE                   the key area's size in bytes
//   key_store.read() -> bytes        the whole key area (blank bytes read 0xFF)
//   key_store.write(offset, data)    program ``data`` at ``offset`` in it. Both a multiple of
//                                    32 bytes (one flash word on every backend), inside the area,
//                                    and every byte there still blank -- else OSError (EINVAL,
//                                    EEXIST) and nothing written. EIO if the flash refused.
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

#define KEY_AREA_SIZE   (256)
#define KEY_WRITE_UNIT  (32)
#define KEY_BLANK       (0xFF)

#if defined(OMV_KEY_STORE_HOST_TEST)

extern uint8_t omv_key_store_host_area[];       // the host test's stand-in for the flash
int omv_key_store_program(uintptr_t addr, const uint8_t *src, size_t len);

#else
#include "py/mphal.h"                            // the port's HAL: defines its family (STM32H7, ...)
#endif

#if defined(OMV_KEY_STORE_HOST_TEST)
#elif defined(STM32F4) || defined(STM32F7) || defined(STM32H7)

#include "flash.h"

// MicroPython's own internal-flash programmer: a word at a time on F4/F7, a 256-bit flash word
// at a time on H7 (KEY_WRITE_UNIT keeps every write whole and aligned for both).
static int omv_key_store_program(uintptr_t addr, const uint8_t *src, size_t len) {
    uint32_t words[KEY_WRITE_UNIT / 4];
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

#else
#error "key_store: no flash backend for this port"
#endif

// Program ``len`` bytes of ``src`` at ``offset`` in the key area: 0, or -22 (EINVAL: not whole
// write units, or outside the area), -17 (EEXIST: not blank there) or -5 (EIO: flash refused).
int omv_key_store_write(size_t offset, const uint8_t *src, size_t len) {
    if (len == 0 || offset % KEY_WRITE_UNIT || len % KEY_WRITE_UNIT ||
        offset > KEY_AREA_SIZE || len > KEY_AREA_SIZE - offset) {
        return -22;
    }
    const volatile uint8_t *dst = (const volatile uint8_t *)(OMV_KEY_AREA_ADDR + offset);
    for (size_t i = 0; i < len; i++) {
        if (dst[i] != KEY_BLANK) {
            return -17;
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

static mp_obj_t mod_key_store_read(void) {
    return mp_obj_new_bytes((const uint8_t *)OMV_KEY_AREA_ADDR, KEY_AREA_SIZE);
}
static MP_DEFINE_CONST_FUN_OBJ_0(key_store_read_obj, mod_key_store_read);

static const mp_rom_map_elem_t key_store_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_key_store) },
    { MP_ROM_QSTR(MP_QSTR_SIZE),     MP_ROM_INT(KEY_AREA_SIZE) },
    { MP_ROM_QSTR(MP_QSTR_read),     MP_ROM_PTR(&key_store_read_obj) },
    { MP_ROM_QSTR(MP_QSTR_write),    MP_ROM_PTR(&key_store_write_obj) },
};
static MP_DEFINE_CONST_DICT(key_store_globals, key_store_globals_table);

const mp_obj_module_t key_store_module = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&key_store_globals,
};
MP_REGISTER_MODULE(MP_QSTR_key_store, key_store_module);

#endif // !OMV_KEY_STORE_HOST_TEST

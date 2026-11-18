/*
 * OpenMV boot stub -- Alif Ensemble port (the OpenMV AE3's Cortex-M55 HP core).
 *
 * The bootloader hands over with the caches on, the MPU holding one region (the MRAM,
 * read-only, write-through) and the OSPI part reset. The stub runs from SRAM0, keeps the
 * caches, maps the MRAM writable (still write-through: a 16-byte line reaches the array as
 * it is stored, and nothing sits dirty in a cache), reads the slots through the OSPI
 * controller, and hands the firmware the same state the bootloader would have.
 */
#include "stub.h"
#include "port.h"
#include "port_alif.h"
#include "sys_ctrl_uart.h"
#include "wdt.h"
#ifdef STUB_SLOT_CRYPT
#include "ecb_slot.h"
#endif

static volatile uint32_t ticks_ms;
static bool prog_mode = false;

// MPU attribute indexes (as the bootloader's).
#define ATTR_DEVICE                 (0)         // nGnRE
#define ATTR_NORMAL_WT_RA           (1)         // write-through, read-allocate
#define ATTR_NORMAL_WB_RA_WA        (2)

void SysTick_Handler(void) {
    ticks_ms += 1;
}

// Every fault escalates to HardFault (the configurable fault handlers stay disabled). Say
// where it happened and park: the UART is open by then on a bench build, and the RAM ring
// has it either way. Alif's SE gates SWD on this core, so this is the only account there is.
void stub_fault(uint32_t *frame) {
    log_kv("fault pc", frame[6]);
    log_kv("fault lr", frame[5]);
    log_kv("fault cfsr", SCB->CFSR);
    log_kv("fault hfsr", SCB->HFSR);
    log_kv("fault bfar", SCB->BFAR);
    log_kv("fault mmfar", SCB->MMFAR);
    while (1) {
        port_led_blink(25);
    }
}

__attribute__((naked)) void HardFault_Handler(void) {
    __asm volatile (
        "tst lr, #4\n"
        "ite eq\n"
        "mrseq r0, msp\n"
        "mrsne r0, psp\n"
        "b stub_fault\n");
}

// Called by start.s from the MRAM-resident head, before the image is copied to SRAM. The
// copy's source (MRAM) is write-through and its destination (SRAM0 in the default map)
// too, so a copy made through the live caches is what the I-cache then fetches: nothing
// to undo here.
__attribute__((section(".flash_head.prep"), used, noinline))
void stub_head_prep(void) {
}

static void mpu_attrs(void) {
    ARM_MPU_SetMemAttr(ATTR_DEVICE, ARM_MPU_ATTR(ARM_MPU_ATTR_DEVICE, ARM_MPU_ATTR_DEVICE_nGnRE));
    ARM_MPU_SetMemAttr(ATTR_NORMAL_WT_RA, ARM_MPU_ATTR(ARM_MPU_ATTR_MEMORY_(0, 0, 1, 0),
                                                       ARM_MPU_ATTR_MEMORY_(0, 0, 1, 0)));
    ARM_MPU_SetMemAttr(ATTR_NORMAL_WB_RA_WA, ARM_MPU_ATTR(ARM_MPU_ATTR_MEMORY_(1, 1, 1, 1),
                                                          ARM_MPU_ATTR_MEMORY_(1, 1, 1, 1)));
}

static void mpu_clear(void) {
    ARM_MPU_Disable();
    for (uint32_t i = 0; i < (MPU->TYPE >> 8); i++) {
        ARM_MPU_ClrRegion(i);
    }
}

// The stub's view: its SRAMs, the peripherals, and the MRAM writable.
static void mpu_setup(void) {
    static const ARM_MPU_Region_t table[] = {
        {   // SRAM0 (code): RW, executable
            .RBAR = ARM_MPU_RBAR(0x02000000, ARM_MPU_SH_NON, 0, 1, 0),
            .RLAR = ARM_MPU_RLAR(0x023FFFFF, ATTR_NORMAL_WT_RA) },
        {   // SRAM1 (buffers): RW, no execute
            .RBAR = ARM_MPU_RBAR(0x08000000, ARM_MPU_SH_NON, 0, 1, 1),
            .RLAR = ARM_MPU_RLAR(0x0827FFFF, ATTR_NORMAL_WB_RA_WA) },
        {   // host peripherals (pin mux)
            .RBAR = ARM_MPU_RBAR(0x1A000000, ARM_MPU_SH_NON, 0, 1, 1),
            .RLAR = ARM_MPU_RLAR(0x1AFFFFFF, ATTR_DEVICE) },
        {   // MRAM: RW (the install window is programmed through the map), write-through
            .RBAR = ARM_MPU_RBAR(STUB_MRAM_START, ARM_MPU_SH_NON, 0, 1, 0),
            .RLAR = ARM_MPU_RLAR(STUB_MRAM_END - 1, ATTR_NORMAL_WT_RA) },
        {   // OSPI controller + AES
            .RBAR = ARM_MPU_RBAR(0x83000000, ARM_MPU_SH_NON, 0, 1, 1),
            .RLAR = ARM_MPU_RLAR(0x83FFFFFF, ATTR_DEVICE) },
    };
    uint32_t primask = __get_PRIMASK();
    __disable_irq();
    mpu_clear();
    mpu_attrs();
    ARM_MPU_Load(0, table, sizeof(table) / sizeof(table[0]));
    ARM_MPU_Enable(MPU_CTRL_PRIVDEFENA_Msk | MPU_CTRL_HFNMIENA_Msk);
    __set_PRIMASK(primask);
}

// The bootloader's hand-over view: no regions but the MRAM, read-only and executable
// (boot/src/common/mpu.c port_mpu_deinit with boot_config.h's XIP partition).
static void mpu_handover(void) {
    uint32_t primask = __get_PRIMASK();
    __disable_irq();
    mpu_clear();
    mpu_attrs();
    MPU->RNR = 4;
    MPU->RBAR = ARM_MPU_RBAR(STUB_MRAM_START, ARM_MPU_SH_NON, 1, 1, 0);
    MPU->RLAR = ARM_MPU_RLAR(STUB_MRAM_END - 1, ATTR_NORMAL_WT_RA);
    ARM_MPU_Enable(MPU_CTRL_PRIVDEFENA_Msk | MPU_CTRL_HFNMIENA_Msk);
    __set_PRIMASK(primask);                     // the firmware starts with interrupts ENABLED, as
                                                // from the bootloader: its ticks and USB need them
}

static void pin_output(uint32_t index, uint32_t state) {
    const stub_pin_t *p = &stub_pins[index];
    port_pin_write(index, state);
    gpio_set_direction_output(p->gpio, p->pin);
}

int port_init(void) {
    // Nothing of ours was in the caches before the copy; make sure nothing stale is now.
    SCB_CleanInvalidateDCache();
    SCB_InvalidateICache();
    mpu_setup();

    SysTick_Config(STUB_CPU_CLOCK / 1000);
    NVIC_SetPriority(SysTick_IRQn, 0);
    #ifdef STUB_DEBUG_UART
    log_open(STUB_DEBUG_UART, 115200);          // a bench build: hear the port come up too
    #endif

    for (size_t i = 0; i < STUB_PINS_COUNT; i++) {
        const stub_pin_t *pin = &stub_pins[i];
        pinconf_set(pin->port, pin->pin, pin->alt, pin->pad);
    }
    pin_output(STUB_LED_PIN, 1);                // off (active low)
    pin_output(STUB_OSPI_RST_PIN, 0);

    // The slots are behind the controller: up on every boot, down again at the jump.
    int ret = ospi_flash_init();
    log_kv("ospi init", ret);
    return 0;
}

void port_cache_clean(const void *p, uint32_t n) {
    SCB_CleanDCache_by_Addr((void *) ((uintptr_t) p & ~31U), n + ((uintptr_t) p & 31U));
}

uint32_t port_ticks_ms(void) {
    return ticks_ms;
}

void port_delay_ms(uint32_t ms) {
    uint32_t start = ticks_ms;
    while ((ticks_ms - start) < ms) {
        __WFI();
    }
}

void port_pin_write(uint32_t index, uint32_t state) {
    const stub_pin_t *p = &stub_pins[index];
    if (state) {
        gpio_set_value_high(p->gpio, p->pin);
    } else {
        gpio_set_value_low(p->gpio, p->pin);
    }
}

void port_led(bool on) {
    port_pin_write(STUB_LED_PIN, !on);          // active low
}

void port_led_blink(uint32_t interval_ms) {
    static uint32_t start_ms = 0;
    static bool state = false;
    if ((ticks_ms - start_ms) < interval_ms) {
        return;
    }
    state = !state;
    port_led(state);
    start_ms = ticks_ms;
}

// ---- Flash ------------------------------------------------------------------

static bool in_range(uint32_t addr, uint32_t size, uint32_t start, uint32_t end) {
    return addr >= start && addr < end && size <= end - addr;
}

static bool in_mram(uint32_t addr, uint32_t size) {
    return in_range(addr, size, STUB_MRAM_START, STUB_MRAM_END);
}

static bool in_ospi(uint32_t addr, uint32_t size) {
    return in_range(addr, size, STUB_OSPI_XIP_BASE, STUB_OSPI_XIP_BASE + STUB_OSPI_SIZE);
}

// MRAM is programmed in 16-byte lines, each as two 64-bit stores back to back. An
// interrupt taken between the two wedges the MRAM controller, and only a power cycle
// recovers (Alif's driver note; the fleet found it the hard way in the coprocessor-romfs
// write). So interrupts are off per 256-byte run, as the firmware does it, and a barrier
// closes each run. The map is write-through: the stores reach the array in order and the
// cache holds nothing dirty; what it does hold for the range is dropped afterwards so the
// read-back sees the array.
static int mram_program(uint32_t addr, const uint8_t *src, uint32_t size) {
    static const uint8_t erased[16] = { [0 ... 15] = 0xFF };
    for (uint32_t off = 0; off < size; off += 256) {
        uint32_t n = (size - off > 256) ? 256 : size - off;
        uint32_t primask = __get_PRIMASK();
        __disable_irq();
        for (uint32_t i = 0; i < n; i += 16) {
            uint64_t line[2];
            memcpy(line, src ? src + off + i : erased, 16);
            volatile uint64_t *dst = (volatile uint64_t *) (addr + off + i);
            dst[0] = line[0];
            dst[1] = line[1];
        }
        __DSB();
        __set_PRIMASK(primask);
    }
    SCB_InvalidateDCache_by_Addr((void *) addr, size);
    return 0;
}

#ifdef STUB_SLOT_CRYPT
// The slots are ciphertext at rest, and the chip's engine only deciphers what is read
// through the memory map -- which the stub never uses. So the cipher is run here, both
// ways, over the one range a slot is selected on: the body of the slot being verified,
// booted or written. Everything else on the part (the other slot, every slot's status
// sector and trailer, /flash) is read and written as it is stored.
static bool crypt_keyed = false;
static uint8_t crypt_flash_key[16];
static ecb_slot_t crypt_slot;
static uint32_t crypt_start = 0, crypt_end = 0;             // [start, end): empty when equal
static uint8_t crypt_chunk[256] __attribute__((aligned(4)));

_Static_assert((STUB_WRITE_UNIT % 16) == 0, "the cipher works on whole 16-byte blocks");

int port_crypt_init(void) {
    // The camera's own key, from the key area in MRAM. A camera that has not made it yet
    // (its first boot) is not an error: its slots run as flashed until it has.
    crypt_keyed = flash_key_read((const uint8_t *) STUB_KEY_AREA, crypt_flash_key);
    return 0;
}

bool port_crypt_keyed(void) {
    return crypt_keyed;
}

int port_crypt_slot_select(const uint8_t *tweak, uint32_t start, uint32_t end) {
    crypt_start = crypt_end = 0;
    if (tweak == NULL) {
        return 0;
    }
    if (!crypt_keyed || (start % 16) != 0 || (end % 16) != 0 || end < start || !in_ospi(start, end - start)) {
        return -1;
    }
    uint8_t key[16];
    ecb_slot_key(crypt_flash_key, tweak, key);
    ecb_slot_init(&crypt_slot, key);
    memset(key, 0, sizeof(key));
    crypt_start = start;
    crypt_end = end;
    return 0;
}

// Nothing of the key stays in RAM for the firmware to find: it reads its own copy.
void port_crypt_lock(void) {
    memset(crypt_flash_key, 0, sizeof(crypt_flash_key));
    memset(&crypt_slot, 0, sizeof(crypt_slot));
    crypt_start = crypt_end = 0;
}

// How much of [addr, addr + size) lies on one side of the ciphered range's edges, and
// whether that piece is inside it.
static uint32_t crypt_piece(uint32_t addr, uint32_t size, bool *inside) {
    *inside = addr >= crypt_start && addr < crypt_end;
    uint32_t room = size;
    if (*inside) {
        room = crypt_end - addr;
    } else if (addr < crypt_start) {
        room = crypt_start - addr;
    }
    return (room < size) ? room : size;
}

// Deciphered bytes of the selected range: whole blocks straight into the buffer, a block
// the read only partly covers through a copy of it.
static int crypt_read(uint32_t addr, uint8_t *buf, uint32_t size) {
    while (size) {
        uint32_t off = addr % 16, n;
        if (off != 0 || size < 16) {
            uint8_t block[16];
            if (ospi_flash_read(addr - off - STUB_OSPI_XIP_BASE, block, 16) != 0) {
                return -1;
            }
            ecb_slot_decrypt(&crypt_slot, block, 16);
            n = (16 - off < size) ? 16 - off : size;
            memcpy(buf, block + off, n);
        } else {
            n = size & ~15U;
            if (ospi_flash_read(addr - STUB_OSPI_XIP_BASE, buf, n) != 0) {
                return -1;
            }
            ecb_slot_decrypt(&crypt_slot, buf, n);
        }
        addr += n;
        buf += n;
        size -= n;
    }
    return 0;
}

int port_flash_read_stored(uint32_t addr, uint8_t *buf, uint32_t size) {
    if (in_mram(addr, size)) {
        memcpy(buf, (const void *) addr, size);
        return 0;
    }
    if (in_ospi(addr, size)) {
        return ospi_flash_read(addr - STUB_OSPI_XIP_BASE, buf, size);
    }
    return -1;
}
#endif

int port_flash_read(uint32_t addr, uint8_t *buf, uint32_t size) {
    if (in_mram(addr, size)) {
        memcpy(buf, (const void *) addr, size);
        return 0;
    }
    if (!in_ospi(addr, size)) {
        return -1;
    }
    #ifdef STUB_SLOT_CRYPT
    while (size) {
        bool inside;
        uint32_t n = crypt_piece(addr, size, &inside);
        if ((inside ? crypt_read(addr, buf, n) : ospi_flash_read(addr - STUB_OSPI_XIP_BASE, buf, n)) != 0) {
            return -1;
        }
        addr += n;
        buf += n;
        size -= n;
    }
    return 0;
    #else
    return ospi_flash_read(addr - STUB_OSPI_XIP_BASE, buf, size);
    #endif
}

int port_flash_prog_begin(void) {
    // MRAM is always writable through the map and the OSPI controller is up since
    // port_init: nothing changes mode here.
    prog_mode = true;
    return 0;
}

int port_flash_prog_end(void) {
    prog_mode = false;
    return 0;
}

int port_flash_erase(uint32_t addr) {
    if (!prog_mode || (addr % STUB_FLASH_BLOCK) != 0) {
        return -1;
    }
    // No erase blocks in MRAM: an erased block reads 0xFF because it was written so.
    if (in_mram(addr, STUB_FLASH_BLOCK)) {
        return mram_program(addr, NULL, STUB_FLASH_BLOCK);
    }
    // A slot's block on the OSPI part: one of its 4 KiB sectors.
    if (in_ospi(addr, STUB_FLASH_BLOCK)) {
        return ospi_flash_erase(addr - STUB_OSPI_XIP_BASE);
    }
    return -1;
}

int port_flash_write(uint32_t addr, const uint8_t *buf, uint32_t size) {
    if (!prog_mode || (addr % STUB_WRITE_UNIT) != 0 || (size % STUB_WRITE_UNIT) != 0) {
        return -1;
    }
    if (in_mram(addr, size)) {
        return mram_program(addr, buf, size);
    }
    if (!in_ospi(addr, size)) {
        return -1;
    }
    #ifdef STUB_SLOT_CRYPT
    while (size) {
        bool inside;
        uint32_t n = crypt_piece(addr, size, &inside);
        if (inside) {
            // What is stored there is ciphertext: a copy of the bytes, ciphered, is what
            // reaches the part.
            if (n > sizeof(crypt_chunk)) {
                n = sizeof(crypt_chunk);
            }
            memcpy(crypt_chunk, buf, n);
            ecb_slot_encrypt(&crypt_slot, crypt_chunk, n);
        }
        if (ospi_flash_write(addr - STUB_OSPI_XIP_BASE, inside ? crypt_chunk : buf, n) != 0) {
            return -1;
        }
        addr += n;
        buf += n;
        size -= n;
    }
    return 0;
    #else
    return ospi_flash_write(addr - STUB_OSPI_XIP_BASE, buf, size);
    #endif
}

// ---- Debug UART --------------------------------------------------------------

static UART_Type *uart_inst = NULL;

int port_uart_init(uint32_t id, uint32_t baud) {
    const stub_uart_t *u = NULL;
    for (size_t i = 0; i < STUB_UARTS_COUNT; i++) {
        if (stub_uarts[i].id == id) {
            u = &stub_uarts[i];
        }
    }
    if (u == NULL || baud == 0 || STUB_UART_CLOCK / baud < 16) {
        return -1;
    }
    pinconf_set(u->port, u->pin, u->alt, PADCTRL_OUTPUT_DRIVE_STRENGTH_8MA | PADCTRL_SLEW_RATE_FAST);

    // Opened twice on the same UART (a bench build's early open, then the trailer's): let the
    // bytes in flight out before the reset below eats them.
    UART_Type *uart = u->uart;
    if (uart == uart_inst) {
        uint32_t start = ticks_ms;
        while (!(uart->UART_LSR & UART_LSR_TRANSMITTER_EMPTY) && (ticks_ms - start) < 20) {
        }
    }

    // As the firmware's mp_uart_init: the peripheral clock, 8N1, FIFOs, no flow control.
    select_uart_clock_syst_pclk(u->id);
    enable_uart_clock(u->id);
    uart_software_reset(uart);
    uart_enable_fifo(uart);
    uart_disable_tx_irq(uart);
    uart_disable_rx_irq(uart);
    uart_set_baudrate(uart, STUB_UART_CLOCK, baud);
    uart_set_data_parity_stop_bits(uart, UART_DATA_BITS_8, UART_PARITY_NONE, UART_STOP_BITS_1);
    uart_set_flow_control(uart, UART_FLOW_CONTROL_NONE);
    uart_inst = uart;
    return 0;
}

void port_uart_putc(char c) {
    if (uart_inst == NULL) {
        return;
    }
    uint32_t start = ticks_ms;
    while (!(uart_inst->UART_LSR & UART_LSR_TRANSMIT_HOLDING_REG_EMPTY)) {
        if ((ticks_ms - start) > 5) {
            return;                             // drop the byte; the RAM ring still has it
        }
    }
    uart_inst->UART_THR = (uint8_t) c;
}

// ---- Watchdog ----------------------------------------------------------------

int port_wdt_start(uint32_t id, uint32_t timeout_ms) {
    if (id != STUB_WDT_HP_ID || timeout_ms == 0) {
        return -1;
    }
    // Counts the core clock; the 32-bit counter caps the timeout near 10 s at 400 MHz
    // (machine.WDT refuses longer; the stub arms the most it can).
    uint64_t ticks = (uint64_t) timeout_ms * (STUB_CPU_CLOCK / 1000);
    if (ticks > 0xFFFFFFFFULL) {
        ticks = 0xFFFFFFFFULL;
    }
    WDT_CTRL_Type *wdt = (WDT_CTRL_Type *) WDT_HP_CTRL_BASE;
    wdt_unlock(wdt);
    wdt_start(wdt, (uint32_t) ticks);
    wdt_lock(wdt);
    return 0;
}

// ---- Jump --------------------------------------------------------------------

void port_jump(uint32_t addr) {
    port_flash_prog_end();
    port_crypt_lock();

    // As the bootloader leaves the OSPI: the controller off, the part hardware-reset. The
    // firmware's driver brings it all up itself.
    ospi_flash_deinit();

    NVIC_DisableIRQ(SysTick_IRQn);
    NVIC_ClearPendingIRQ(SysTick_IRQn);
    SysTick->CTRL &= ~SysTick_CTRL_ENABLE_Msk;

    port_led(false);

    // Caches on and clean -- nothing of the installed firmware in either -- and the MPU as
    // the bootloader hands it over.
    SCB_CleanInvalidateDCache();
    SCB_InvalidateICache();
    mpu_handover();
    __DSB();
    __ISB();

    ((void (*)(void)) (*((uint32_t *) (addr + 4))))();
    while (1) {
    }
}

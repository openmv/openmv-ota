/*
 * OpenMV boot stub -- i.MX RT port (the OpenMV Cam RT1062).
 *
 * The chip's ROM boots NXP's MCU bootloader from the QSPI part; that bootloader checks the
 * vector table at the stub's head and jumps to it with the caches on, the MPU off and
 * interrupts enabled. The stub runs from OCRAM2, reads the slots and the firmware area
 * through the FlexSPI memory map, and programs the part with the ROM's own FlexSPI NOR
 * driver -- the one that read the FCB at boot -- fed the same FCB. The firmware is handed
 * the state its startup expects from the bootloader: caches off and clean, MPU off,
 * interrupts on, VTOR and the stack from its own vector table.
 */
#include <string.h>
#include "stub.h"
#include "port.h"
#include "port_mimxrt.h"
#include "fsl_romapi.h"
#include "system_MIMXRT1062.h"

static volatile uint32_t ticks_ms;
static bool prog_mode = false;

// The region's blank lead-in: the FCB and IVT slots of the image this region used to hold.
// The bootloader never reads them (it jumps to the vectors at STUB_FLASH_HEAD_OFFSET), so
// they stay erased.
__attribute__((section(".flash_pad"), used))
static const uint8_t flash_pad[STUB_FLASH_HEAD_OFFSET] = { [0 ... STUB_FLASH_HEAD_OFFSET - 1] = 0xFF };

void SysTick_Handler(void) {
    ticks_ms += 1;
}

// Every fault escalates to HardFault (the configurable fault handlers stay disabled). Say
// where it happened and park: the UART is open by then on a bench build, and the RAM ring
// has it either way.
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

// Called by start.s from the flash-resident head, before the image is copied to OCRAM. The
// default memory map has OCRAM write-back cacheable: a copy made through a live D-cache can
// sit dirty there while the I-cache fetches the stale array. So the D-cache goes off (clean
// and invalidate, as CMSIS does on disable) and the MPU with it -- the bootloader's view, if
// it had one, is not the stub's. port_init turns the caches back on over the copy.
__attribute__((section(".flash_head.prep"), used, noinline))
void stub_head_prep(void) {
    ARM_MPU_Disable();
    if (SCB->CCR & SCB_CCR_DC_Msk) {
        SCB_DisableDCache();
    }
    SCB_InvalidateICache();
}

static void pin_mux(const stub_pin_t *p, uint32_t pad) {
    IOMUXC->SW_MUX_CTL_PAD[p->mux] = IOMUXC_SW_MUX_CTL_PAD_MUX_MODE(p->alt);
    IOMUXC->SW_PAD_CTL_PAD[p->mux] = pad;
}

int port_init(void) {
    // Caches on over the OCRAM copy (each enable invalidates first).
    SCB_EnableICache();
    SCB_EnableDCache();
    // What the firmware's SystemInit does first: the watchdogs' power-down counters off (a
    // fresh chip bites 16 s after power-on otherwise) and the RTWDOG disabled. Idempotent.
    WDOG1->WMCR &= (uint16_t) ~WDOG_WMCR_PDE_MASK;
    WDOG2->WMCR &= (uint16_t) ~WDOG_WMCR_PDE_MASK;
    RTWDOG->CNT = 0xD928C520U;                  // unlock
    RTWDOG->TOVAL = 0xFFFF;
    RTWDOG->CS = (RTWDOG->CS & ~RTWDOG_CS_EN_MASK) | RTWDOG_CS_UPDATE_MASK;
    SystemCoreClockUpdate();
    SysTick_Config(SystemCoreClock / 1000);
    NVIC_SetPriority(SysTick_IRQn, 0);
    #ifdef STUB_DEBUG_UART
    log_open(STUB_DEBUG_UART, 115200);          // a bench build: hear the port come up too
    #endif
    log_kv("core hz", SystemCoreClock);
    // The LED: its pad to the GPIO module (not the fast-GPIO alias), an output, off.
    const stub_pin_t *led = &stub_pins[STUB_LED_PIN];
    IOMUXC_GPR->GPR27 &= ~(1U << led->pin);
    pin_mux(led, IOMUXC_SW_PAD_CTL_PAD_DSE(6) | IOMUXC_SW_PAD_CTL_PAD_SPEED(2));
    led->gpio->DR |= 1U << led->pin;
    led->gpio->GDIR |= 1U << led->pin;
    return 0;
}

void port_cache_clean(const void *p, uint32_t n) {
    SCB_CleanDCache_by_Addr((void *) ((uintptr_t) p & ~31U), n + ((uintptr_t) p & 31U));
}

uint32_t port_ticks_ms(void) {
    return ticks_ms;
}

void port_led(bool on) {
    const stub_pin_t *led = &stub_pins[STUB_LED_PIN];
    if (on) {
        led->gpio->DR &= ~(1U << led->pin);     // active low
    } else {
        led->gpio->DR |= 1U << led->pin;
    }
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
//
// Reads go through the memory map. Erase and program go through the ROM's FlexSPI NOR driver
// with the FCB the ROM booted from (the first 512 bytes of the part), the one configuration
// that is known to describe this part; the ROM API wants it in RAM, 4-byte aligned. Each
// operation runs with interrupts off (nothing else may touch the controller mid-command) and
// is followed by a flush of the controller's AHB read buffer and of the D-cache lines over the
// range, so the next memory-mapped read sees the array.

static flexspi_nor_config_t nor_cfg __attribute__((aligned(4)));
static bool nor_ready = false;
static uint32_t page_buf[STUB_FLASH_PAGE / 4] __attribute__((aligned(4)));

static bool in_range(uint32_t addr, uint32_t size, uint32_t start, uint32_t end) {
    return addr >= start && addr < end && size <= end - addr;
}

static bool in_flash(uint32_t addr, uint32_t size) {
    return in_range(addr, size, STUB_XIP_BASE, STUB_XIP_BASE + STUB_FLASH_SIZE);
}

static int nor_init(void) {
    if (nor_ready) {
        return 0;
    }
    memcpy(&nor_cfg, (const void *) STUB_XIP_BASE, sizeof(nor_cfg));
    if (nor_cfg.memConfig.tag != FLEXSPI_CFG_BLK_TAG) {
        log_kv("fcb tag", nor_cfg.memConfig.tag);
        return -1;
    }
    if (nor_cfg.pageSize != STUB_FLASH_PAGE || nor_cfg.sectorSize != STUB_FLASH_BLOCK) {
        log_kv("fcb page", nor_cfg.pageSize);   // not the geometry this port is built for
        log_kv("fcb sector", nor_cfg.sectorSize);
        return -1;
    }
    uint32_t primask = __get_PRIMASK();
    __disable_irq();
    status_t st = ROM_FLEXSPI_NorFlash_Init(STUB_FLEXSPI_INSTANCE, &nor_cfg);
    ROM_FLEXSPI_NorFlash_ClearCache(STUB_FLEXSPI_INSTANCE);
    __set_PRIMASK(primask);
    log_kv("nor init", (uint32_t) st);
    if (st != kStatus_Success) {
        return -1;
    }
    nor_ready = true;
    return 0;
}

static void nor_sync(uint32_t addr, uint32_t size) {
    ROM_FLEXSPI_NorFlash_ClearCache(STUB_FLEXSPI_INSTANCE);
    SCB_InvalidateDCache_by_Addr((void *) addr, size);
}

int port_flash_read(uint32_t addr, uint8_t *buf, uint32_t size) {
    if (!in_flash(addr, size)) {
        return -1;
    }
    memcpy(buf, (const void *) addr, size);
    return 0;
}

int port_flash_prog_begin(void) {
    // The fast path never gets here: the ROM driver is brought up on the first install or
    // attempt mark, and stays up (it is the state the ROM left anyway).
    if (nor_init() != 0) {
        return -1;
    }
    prog_mode = true;
    return 0;
}

int port_flash_prog_end(void) {
    prog_mode = false;
    return 0;
}

int port_flash_erase(uint32_t addr) {
    if (!prog_mode || (addr % STUB_FLASH_BLOCK) != 0 || !in_flash(addr, STUB_FLASH_BLOCK)) {
        return -1;
    }
    uint32_t primask = __get_PRIMASK();
    __disable_irq();
    status_t st = ROM_FLEXSPI_NorFlash_Erase(STUB_FLEXSPI_INSTANCE, &nor_cfg,
                                             addr - STUB_XIP_BASE, STUB_FLASH_BLOCK);
    nor_sync(addr, STUB_FLASH_BLOCK);
    __set_PRIMASK(primask);
    return (st == kStatus_Success) ? 0 : -1;
}

// 16-byte units anywhere in a page. The ROM driver programs whole pages, so a unit goes in
// a page image padded with 0xFF: a NOR program only clears bits, and 0xFF clears none, so the
// rest of the page is untouched (that is how the status marks land beside each other).
int port_flash_write(uint32_t addr, const uint8_t *buf, uint32_t size) {
    if (!prog_mode || (addr % STUB_WRITE_UNIT) != 0 || (size % STUB_WRITE_UNIT) != 0
        || !in_flash(addr, size)) {
        return -1;
    }
    while (size > 0) {
        uint32_t off = addr % STUB_FLASH_PAGE;
        uint32_t n = (size < STUB_FLASH_PAGE - off) ? size : STUB_FLASH_PAGE - off;
        memset(page_buf, 0xFF, sizeof(page_buf));
        memcpy((uint8_t *) page_buf + off, buf, n);
        uint32_t primask = __get_PRIMASK();
        __disable_irq();
        status_t st = ROM_FLEXSPI_NorFlash_ProgramPage(STUB_FLEXSPI_INSTANCE, &nor_cfg,
                                                       (addr - off) - STUB_XIP_BASE, page_buf);
        nor_sync(addr - off, STUB_FLASH_PAGE);
        __set_PRIMASK(primask);
        if (st != kStatus_Success) {
            return -1;
        }
        addr += n;
        buf += n;
        size -= n;
    }
    return 0;
}

// ---- Debug UART --------------------------------------------------------------

static LPUART_Type *uart_inst = NULL;

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
    LPUART_Type *uart = u->uart;
    // Opened twice on the same UART (a bench build's early open, then the trailer's): let the
    // bytes in flight out before the reset below eats them.
    if (uart == uart_inst) {
        uint32_t start = ticks_ms;
        while (!(uart->STAT & LPUART_STAT_TC_MASK) && (ticks_ms - start) < 20) {
        }
    }
    // The UART root clock: the 24 MHz oscillator, undivided -- always running, whatever PLL
    // state the bootloader left. The firmware's startup rebuilds the clock tree anyway.
    CCM->CSCDR1 = (CCM->CSCDR1 & ~(CCM_CSCDR1_UART_CLK_SEL_MASK | CCM_CSCDR1_UART_CLK_PODF_MASK))
                  | CCM_CSCDR1_UART_CLK_SEL_MASK;
    *u->ccgr |= u->ccgr_mask;
    IOMUXC->SW_MUX_CTL_PAD[u->mux] = IOMUXC_SW_MUX_CTL_PAD_MUX_MODE(u->alt);
    IOMUXC->SW_PAD_CTL_PAD[u->mux] = IOMUXC_SW_PAD_CTL_PAD_DSE(6) | IOMUXC_SW_PAD_CTL_PAD_SPEED(2)
                                     | IOMUXC_SW_PAD_CTL_PAD_PKE_MASK;
    // 8N1, transmitter only, 16x oversampling.
    uart->GLOBAL |= LPUART_GLOBAL_RST_MASK;
    uart->GLOBAL &= ~LPUART_GLOBAL_RST_MASK;
    uart->BAUD = LPUART_BAUD_OSR(15) | LPUART_BAUD_SBR(STUB_UART_CLOCK / (16 * baud));
    uart->CTRL = LPUART_CTRL_TE_MASK;
    uart_inst = uart;
    return 0;
}

void port_uart_putc(char c) {
    if (uart_inst == NULL) {
        return;
    }
    uint32_t start = ticks_ms;
    while (!(uart_inst->STAT & LPUART_STAT_TDRE_MASK)) {
        if ((ticks_ms - start) > 5) {
            return;                             // drop the byte; the RAM ring still has it
        }
    }
    uart_inst->DATA = (uint8_t) c;
}

// ---- Watchdog ----------------------------------------------------------------

int port_wdt_start(uint32_t id, uint32_t timeout_ms) {
    if (id != STUB_WDT_WDOG1_ID || timeout_ms == 0) {
        return -1;
    }
    // WDOG1 counts half seconds, WT + 1 of them (0.5 s .. 128 s). WDE and the mode bits are
    // write-once: these are the values the firmware's machine.WDT(0) writes too, so its own
    // init later goes through and just reprograms the timeout.
    uint32_t wt = (timeout_ms + 499) / 500;
    wt = (wt > 0) ? wt - 1 : 0;
    if (wt > 0xFF) {
        wt = 0xFF;
    }
    CCM->CCGR3 |= CCM_CCGR3_CG8_MASK;
    WDOG1->WMCR &= (uint16_t) ~WDOG_WMCR_PDE_MASK;
    WDOG1->WCR = WDOG_WCR_WT(wt) | WDOG_WCR_WDE_MASK | WDOG_WCR_SRS_MASK | WDOG_WCR_WDA_MASK;
    WDOG1->WSR = 0x5555;
    WDOG1->WSR = 0xAAAA;
    return 0;
}

// ---- Jump --------------------------------------------------------------------

__attribute__((naked, noreturn)) static void jump_to(uint32_t vectors) {
    __asm volatile (
        "ldr r1, [r0]\n"
        "msr msp, r1\n"
        "ldr r1, [r0, #4]\n"
        "bx r1\n");
}

void port_jump(uint32_t addr) {
    port_flash_prog_end();
    NVIC_DisableIRQ(SysTick_IRQn);
    NVIC_ClearPendingIRQ(SysTick_IRQn);
    SysTick->CTRL &= ~SysTick_CTRL_ENABLE_Msk;
    port_led(false);
    // As the bootloader hands over: caches off and clean, the MPU off (stub_head_prep), the
    // vector table and stack the firmware's own. Interrupts stay enabled, as they were.
    SCB_CleanInvalidateDCache();
    SCB_DisableDCache();
    SCB_DisableICache();
    SCB->VTOR = addr;
    __DSB();
    __ISB();
    jump_to(addr);
}

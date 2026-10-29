/*
 * OpenMV boot stub -- STM32 port.
 *
 * The stub inherits the bootloader's clock tree and leaves it alone, exactly as the
 * firmware does when the bootloader jumps to it. It owns only the flash, the LED, the
 * debug UART and the watchdog.
 *
 * Two flash models share this file. The N6 (STM32N6) runs from and installs into one
 * external octal flash through the bootloader's XSPI driver (xspi.c): reads come off the
 * memory map until an install switches the controller to indirect mode. The H7 boards
 * (STM32H7: the OpenMV Cam H7 Plus / Pure Thermal and the Arduino Portenta, Giga and
 * Nicla Vision) install into the internal flash, programmed a 32-byte flash word at a
 * time, and keep their slots on a QSPI part read in indirect mode (qspi.c) -- there is
 * no memory map to restore, only a controller to leave reset.
 */
#include <string.h>
#include STM32_HAL_H
#include "stub.h"
#include "port.h"
#include "port_stm32.h"
#if defined(STM32N6)
#include "board_config.h"               // the firmware's board: oscillators, PLLs, ICs
#endif

#if defined(IWDG1) && !defined(IWDG)
#define IWDG IWDG1
#endif

uint32_t SystemCoreClock = HSI_VALUE;
#if defined(STM32H7)
uint32_t SystemD2Clock = HSI_VALUE;     // the HAL's RCC code keeps both of these current
const uint8_t D1CorePrescTable[16] = { 0, 0, 0, 0, 1, 2, 3, 4, 1, 2, 3, 4, 6, 7, 8, 9 };
#endif

static bool prog_mode = false;

void SystemCoreClockUpdate(void) {
    #if defined(STM32N6)
    SystemCoreClock = HAL_RCC_GetCpuClockFreq();
    #else
    SystemCoreClock = HAL_RCC_GetSysClockFreq() >>
                      (D1CorePrescTable[(RCC->D1CFGR & RCC_D1CFGR_D1CPRE) >> RCC_D1CFGR_D1CPRE_Pos] & 0x1FU);
    #endif
}

// Called by start.s from the flash-resident head, before the image is copied to SRAM:
// undo what the bootloader left on that would make that copy incoherent. The OpenMV
// bootloader hands over with the caches off; MCUboot on the Arduino boards does not
// promise that, and a copy made through a live D-cache is not what the I-cache then
// fetches. Registers only: nothing of ours is in RAM yet.
__attribute__((section(".flash_head.prep"), used, noinline))
void stub_head_prep(void) {
    #if defined(STM32H7)
    ARM_MPU_Disable();
    if (SCB->CCR & SCB_CCR_DC_Msk) {
        SCB_DisableDCache();            // cleans and invalidates on the way out
    }
    if (SCB->CCR & SCB_CCR_IC_Msk) {
        SCB_DisableICache();
    }
    #endif
}

void SysTick_Handler(void) {
    HAL_IncTick();
}

#define GPIO_CLOCK(port) \
    if (gpio == GPIO##port) { __HAL_RCC_GPIO##port##_CLK_ENABLE(); }

static void gpio_clock(GPIO_TypeDef *gpio) {
    GPIO_CLOCK(A)
    GPIO_CLOCK(B)
    GPIO_CLOCK(C)
    GPIO_CLOCK(D)
    GPIO_CLOCK(E)
    GPIO_CLOCK(F)
    GPIO_CLOCK(G)
    GPIO_CLOCK(H)
    #ifdef GPIOI
    GPIO_CLOCK(I)
    #endif
    #ifdef GPIOJ
    GPIO_CLOCK(J)
    #endif
    #ifdef GPIOK
    GPIO_CLOCK(K)
    #endif
    #ifdef GPION
    GPIO_CLOCK(N)
    #endif
    #ifdef GPIOO
    GPIO_CLOCK(O)
    #endif
    #ifdef GPIOP
    GPIO_CLOCK(P)
    #endif
    #ifdef GPIOQ
    GPIO_CLOCK(Q)
    #endif
}

int port_init(void) {
    // The bootloader left the caches off; make sure nothing stale survives the copy.
    if (SCB->CCR & SCB_CCR_IC_Msk) {
        SCB_InvalidateICache();
    }
    if (SCB->CCR & SCB_CCR_DC_Msk) {
        SCB_CleanInvalidateDCache();
    }

    SystemCoreClockUpdate();
    HAL_Init();
    HAL_NVIC_SetPriority(SysTick_IRQn, 0, 0);
    #if defined(STM32H7)
    SCB->SHCSR |= SCB_SHCSR_BUSFAULTENA_Msk;    // a bad flash read is a BusFault, not a HardFault
    #endif

    for (size_t i = 0; i < STUB_PINS_COUNT; i++) {
        const stub_pin_t *pin = &stub_pins[i];
        GPIO_InitTypeDef cfg = {
            .Pin = pin->pin,
            .Pull = pin->pull,
            .Speed = pin->speed,
            .Mode = pin->mode,
            .Alternate = pin->alt,
        };
        gpio_clock(pin->gpio);
        HAL_GPIO_Init(pin->gpio, &cfg);
    }
    port_led(false);

    SCB_EnableICache();
    #ifndef STUB_DCACHE_OFF
    SCB_EnableDCache();
    #endif
    return 0;
}

void port_cache_clean(const void *p, uint32_t n) {
    if (SCB->CCR & SCB_CCR_DC_Msk) {
        SCB_CleanDCache_by_Addr((void *) ((uintptr_t) p & ~31U), n + ((uintptr_t) p & 31U));
    }
}

uint32_t port_ticks_ms(void) {
    return HAL_GetTick();
}

void port_delay_ms(uint32_t ms) {
    uint32_t start = HAL_GetTick();
    while ((HAL_GetTick() - start) < ms) {
        __WFI();
    }
}

void port_pin_write(uint32_t index, uint32_t state) {
    const stub_pin_t *p = &stub_pins[index];
    HAL_GPIO_WritePin(p->gpio, p->pin, state ? GPIO_PIN_SET : GPIO_PIN_RESET);
}

void port_led(bool on) {
    port_pin_write(STUB_LED_PIN, !on);      // active low
}

void port_led_blink(uint32_t interval_ms) {
    static uint32_t start_ms = 0;
    static bool state = false;
    if ((HAL_GetTick() - start_ms) < interval_ms) {
        return;
    }
    state = !state;
    port_led(state);
    start_ms = HAL_GetTick();
}

// ---- Flash ------------------------------------------------------------------

#if defined(STM32N6)

int port_flash_read(uint32_t addr, uint8_t *buf, uint32_t size) {
    if (!prog_mode) {
        memcpy(buf, (const void *) addr, size);
        return 0;
    }
    return spi_flash_read(addr - STUB_XIP_BASE, buf, size);
}

int port_flash_prog_begin(void) {
    if (prog_mode) {
        return 0;
    }
    int ret;
    if (!spi_flash_initialized()) {
        // First time: out of the bootloader's memory map and into a known flash state --
        // the driver resets the controller and hardware-resets the part (-> 1-line SPI).
        spi_flash_deinit();
        ret = xspi_flash_init();
        log_kv("xspi init", ret);
    } else {
        // Our own memory map: leave it the way the firmware's driver does, in place.
        ret = spi_flash_memory_unmap();
        log_kv("xspi unmap", ret);
    }
    if (ret != 0) {
        return -1;
    }
    prog_mode = true;
    return 0;
}

int port_flash_prog_end(void) {
    if (!prog_mode) {
        return 0;
    }
    if (spi_flash_memory_map(true) != 0) {
        return -1;
    }
    prog_mode = false;
    // The memory map changed under the cache.
    SCB_CleanInvalidateDCache();
    return 0;
}

int port_flash_erase(uint32_t addr) {
    if (!prog_mode || (addr % STUB_FLASH_BLOCK) != 0) {
        return -1;
    }
    return spi_flash_erase_block(addr - STUB_XIP_BASE);
}

int port_flash_write(uint32_t addr, const uint8_t *buf, uint32_t size) {
    if (!prog_mode || (addr % 16) != 0 || (size % 16) != 0) {
        return -1;
    }
    return spi_flash_write(addr - STUB_XIP_BASE, buf, size);
}

#elif defined(STM32H7)

// Internal flash: two banks of eight 128 KiB sectors, programmed in 32-byte flash words,
// read through the bus. The slots live on the QSPI part (qspi.c), by chip offset.
#define AXI_FLASH_BASE          (0x08000000U)
#define AXI_FLASH_SIZE          (0x200000U)

static volatile uint32_t flash_faults = 0;
static volatile bool flash_reading = false;     // inside port_flash_read's copy: a fault is a torn word
static volatile bool jumping = false;           // in port_jump: everything is verified, a stray fault
                                                //   must not stop the boot (seen on the H7 Plus)

static bool in_axi_flash(uint32_t addr, uint32_t size) {
    return addr >= AXI_FLASH_BASE && addr - AXI_FLASH_BASE <= AXI_FLASH_SIZE &&
           size <= AXI_FLASH_SIZE - (addr - AXI_FLASH_BASE);
}

static bool in_qspi(uint32_t addr, uint32_t size) {
    return addr >= STUB_QSPI_BASE && addr - STUB_QSPI_BASE <= STUB_QSPI_FLASH_SIZE &&
           size <= STUB_QSPI_FLASH_SIZE - (addr - STUB_QSPI_BASE);
}

// A flash word whose programming was cut short reads back as a double ECC error: the
// H7 raises an NMI (and a BusFault if the bus returns the error), the handlers below
// count it, and the read that tripped it fails -- "corrupt", so the stub installs again.
// Only a fault inside port_flash_read's copy is read that way; any other fault is a bug
// in the stub and parks the board (the LED cannot blink from a handler), never resumes.
void NMI_Handler(void) {
    if (FLASH->SR1 & FLASH_SR_DBECCERR) {
        FLASH->CCR1 = FLASH_CCR_CLR_DBECCERR;
    }
    if (FLASH->SR2 & FLASH_SR_DBECCERR) {
        FLASH->CCR2 = FLASH_CCR_CLR_DBECCERR;
    }
    flash_faults++;
    while (!flash_reading && !jumping) {
    }
}

// Step over the faulting instruction (16- or 32-bit Thumb) and resume; the load's
// destination holds garbage the caller discards once it sees the fault count move.
void stub_fault_skip(uint32_t *frame) {
    while (!flash_reading && !jumping) {
    }
    uint32_t pc = frame[6];
    uint16_t op = *(const uint16_t *) pc;
    frame[6] = pc + (((op & 0xF800U) >= 0xE800U) ? 4 : 2);
    SCB->CFSR = SCB->CFSR;
    SCB->HFSR = SCB->HFSR;
    flash_faults++;
}

__attribute__((naked)) void BusFault_Handler(void) {
    __asm volatile ("mrs r0, msp\n\tb stub_fault_skip");
}

__attribute__((naked)) void HardFault_Handler(void) {
    __asm volatile ("mrs r0, msp\n\tb stub_fault_skip");
}

int port_flash_read(uint32_t addr, uint8_t *buf, uint32_t size) {
    if (in_axi_flash(addr, size)) {
        uint32_t before = flash_faults;
        flash_reading = true;
        memcpy(buf, (const void *) addr, size);
        __DSB();
        flash_reading = false;
        return (flash_faults == before) ? 0 : -1;
    }
    if (in_qspi(addr, size)) {
        return qspi_flash_read(addr - STUB_QSPI_BASE, buf, size);
    }
    return -1;
}

int port_flash_prog_begin(void) {
    if (prog_mode) {
        return 0;
    }
    HAL_FLASH_Unlock();
    __HAL_FLASH_CLEAR_FLAG(FLASH_FLAG_ALL_ERRORS_BANK1 | FLASH_FLAG_ALL_ERRORS_BANK2);
    prog_mode = true;
    return 0;
}

int port_flash_prog_end(void) {
    if (!prog_mode) {
        return 0;
    }
    HAL_FLASH_Lock();
    prog_mode = false;
    return 0;
}

int port_flash_erase(uint32_t addr) {
    if (!prog_mode || (addr % STUB_FLASH_BLOCK) != 0 || !in_axi_flash(addr, STUB_FLASH_BLOCK)) {
        return -1;
    }
    uint32_t sector = (addr - AXI_FLASH_BASE) / FLASH_SECTOR_SIZE;
    FLASH_EraseInitTypeDef erase = {
        .TypeErase = FLASH_TYPEERASE_SECTORS,
        .Banks = (sector < FLASH_SECTOR_TOTAL) ? FLASH_BANK_1 : FLASH_BANK_2,
        .Sector = sector % FLASH_SECTOR_TOTAL,
        .NbSectors = 1,
        .VoltageRange = FLASH_VOLTAGE_RANGE_3,
    };
    uint32_t error = 0;
    __HAL_FLASH_CLEAR_FLAG(FLASH_FLAG_ALL_ERRORS_BANK1 | FLASH_FLAG_ALL_ERRORS_BANK2);
    return (HAL_FLASHEx_Erase(&erase, &error) == HAL_OK) ? 0 : -1;
}

int port_flash_write(uint32_t addr, const uint8_t *buf, uint32_t size) {
    if (!prog_mode) {
        return -1;
    }
    if (in_axi_flash(addr, size)) {
        if ((addr % STUB_WRITE_UNIT) != 0 || (size % STUB_WRITE_UNIT) != 0 || ((uintptr_t) buf % 4) != 0) {
            return -1;
        }
        for (uint32_t i = 0; i < size; i += STUB_WRITE_UNIT) {
            if (HAL_FLASH_Program(FLASH_TYPEPROGRAM_FLASHWORD, addr + i, (uint32_t) (buf + i)) != HAL_OK) {
                return -1;
            }
        }
        return 0;
    }
    if (in_qspi(addr, size)) {
        return qspi_flash_write(addr - STUB_QSPI_BASE, buf, size);
    }
    return -1;
}

#endif

// ---- Debug UART (TX only, polled) --------------------------------------------
//
// Registers, not HAL_UART_Init: that waits on TEACK with a ~9 hour timeout. The stub
// must never park on its debug output, so every wait here is bounded by the tick.

static USART_TypeDef *uart_inst = NULL;

// Bus clock on; returns the RCC_PERIPHCLK id the HAL reports the kernel clock under.
#define UART_CLOCK(inst, name, periph) \
    if (uart == inst) { \
        __HAL_RCC_##name##_CLK_ENABLE(); \
        return periph; \
    }

static uint64_t uart_bus_clock(USART_TypeDef *uart) {
    #ifdef USART1
    UART_CLOCK(USART1, USART1, RCC_PERIPHCLK_USART1)
    #endif
    #ifdef USART2
    UART_CLOCK(USART2, USART2, RCC_PERIPHCLK_USART2)
    #endif
    #ifdef USART3
    UART_CLOCK(USART3, USART3, RCC_PERIPHCLK_USART3)
    #endif
    #ifdef UART4
    UART_CLOCK(UART4, UART4, RCC_PERIPHCLK_UART4)
    #endif
    #ifdef UART5
    UART_CLOCK(UART5, UART5, RCC_PERIPHCLK_UART5)
    #endif
    #ifdef USART6
    UART_CLOCK(USART6, USART6, RCC_PERIPHCLK_USART6)
    #endif
    #ifdef UART7
    UART_CLOCK(UART7, UART7, RCC_PERIPHCLK_UART7)
    #endif
    #ifdef UART8
    UART_CLOCK(UART8, UART8, RCC_PERIPHCLK_UART8)
    #endif
    return 0;
}

// The clock tree the UART needs, set up the way the firmware sets it up -- from the
// same board macros (board_config.h in the firmware's board directory), so the stub
// follows the board. On the N6 that is the oscillators and PLL3/PLL4 the board names
// (HAL_RCC_OscConfig in system_stm32n6.c) and IC14 from PLL1; then every UART runs
// from IC14 (uart_init0). The bootloader leaves PLL3 off, and with it off the USART
// kernel-clock mux never delivers a clock, whichever source is selected: TEACK never
// rises and the first byte sits in TDR until the firmware turns PLL3 on. PLL1 is left
// alone, like the firmware does: the CPU is running from it.
static void uart_clock_init(void) {
    static bool done = false;
    if (done) {
        return;
    }
    done = true;
    #if defined(STM32N6)
    RCC_OscInitTypeDef osc = {0};
    osc.PLL1.PLLState = RCC_PLL_NONE;
    osc.PLL2.PLLState = RCC_PLL_NONE;
    osc.PLL3.PLLState = RCC_PLL_NONE;
    osc.PLL4.PLLState = RCC_PLL_NONE;
    #if defined(OMV_OSC_PLL3SOURCE)
    osc.PLL3.PLLState = RCC_PLL_ON;
    osc.PLL3.PLLSource = OMV_OSC_PLL3SOURCE;
    osc.PLL3.PLLM = OMV_OSC_PLL3M;
    osc.PLL3.PLLN = OMV_OSC_PLL3N;
    osc.PLL3.PLLP1 = OMV_OSC_PLL3P1;
    osc.PLL3.PLLP2 = OMV_OSC_PLL3P2;
    osc.PLL3.PLLFractional = OMV_OSC_PLL3FRAC;
    #endif
    #if defined(OMV_OSC_PLL4SOURCE)
    osc.PLL4.PLLState = RCC_PLL_ON;
    osc.PLL4.PLLSource = OMV_OSC_PLL4SOURCE;
    osc.PLL4.PLLM = OMV_OSC_PLL4M;
    osc.PLL4.PLLN = OMV_OSC_PLL4N;
    osc.PLL4.PLLP1 = OMV_OSC_PLL4P1;
    osc.PLL4.PLLP2 = OMV_OSC_PLL4P2;
    osc.PLL4.PLLFractional = OMV_OSC_PLL4FRAC;
    #endif
    #if defined(OMV_OSC_LSE_STATE)
    osc.LSEState = OMV_OSC_LSE_STATE;
    osc.OscillatorType |= RCC_OSCILLATORTYPE_LSE;
    #endif
    #if defined(OMV_OSC_HSE_STATE)
    osc.HSEState = OMV_OSC_HSE_STATE;
    osc.OscillatorType |= RCC_OSCILLATORTYPE_HSE;
    #endif
    #if defined(OMV_OSC_LSI_STATE)
    osc.LSIState = OMV_OSC_LSI_STATE;
    osc.OscillatorType |= RCC_OSCILLATORTYPE_LSI;
    #endif
    #if defined(OMV_OSC_HSI_STATE)
    osc.HSIState = OMV_OSC_HSI_STATE;
    osc.OscillatorType |= RCC_OSCILLATORTYPE_HSI;
    osc.HSIDiv = OMV_OSC_HSI_DIV;
    osc.HSICalibrationValue = OMV_OSC_HSI_CAL;
    #endif
    (void) HAL_RCC_OscConfig(&osc);            // a failure leaves the log silent, nothing more
    #if defined(OMV_RCC_IC14_SOURCE)
    LL_RCC_IC14_SetSource(OMV_RCC_IC14_SOURCE);
    LL_RCC_IC14_SetDivider(OMV_RCC_IC14_CLKDIV);
    LL_RCC_IC14_Enable();
    #endif
    #endif
}

int port_uart_init(uint32_t id, uint32_t baud) {
    const stub_uart_t *u = NULL;
    for (size_t i = 0; i < STUB_UARTS_COUNT; i++) {
        if (stub_uarts[i].id == id) {
            u = &stub_uarts[i];
        }
    }
    if (u == NULL || baud == 0) {
        return -1;
    }
    uart_clock_init();
    uint64_t periph = uart_bus_clock(u->uart);
    if (periph == 0) {
        return -1;
    }
    LL_RCC_SetClockSource(u->clk);
    #if defined(STM32H7)
    // The H7 HAL's HAL_RCCEx_GetPeriphCLKFreq knows no USART, so with the kernel clock set
    // to the bus clock above, read that: APB2 for USART1/6, APB1 for the rest.
    (void) periph;
    uint32_t clk = (u->uart == USART1 || u->uart == USART6) ? HAL_RCC_GetPCLK2Freq()
                                                             : HAL_RCC_GetPCLK1Freq();
    #else
    uint32_t clk = HAL_RCCEx_GetPeriphCLKFreq(periph);
    #endif
    if (clk == 0 || clk / baud < 16 || clk / baud > 0xFFFF) {
        return -1;
    }
    gpio_clock(u->gpio);
    GPIO_InitTypeDef cfg = {
        .Pin = u->pin,
        .Mode = GPIO_MODE_AF_PP,
        .Pull = GPIO_PULLUP,
        .Speed = GPIO_SPEED_FREQ_LOW,
        .Alternate = u->alt,
    };
    HAL_GPIO_Init(u->gpio, &cfg);

    USART_TypeDef *uart = u->uart;
    uart->CR1 = 0;
    uart->CR2 = 0;
    uart->CR3 = 0;
    uart->PRESC = 0;
    uart->BRR = (clk + baud / 2) / baud;        // 8N1, oversampling 16
    uart->CR1 = USART_CR1_TE | USART_CR1_UE;
    uart_inst = uart;
    return 0;
}

void port_uart_putc(char c) {
    if (uart_inst == NULL) {
        return;
    }
    uint32_t start = HAL_GetTick();
    while (!(uart_inst->ISR & USART_ISR_TXE_TXFNF)) {
        if ((HAL_GetTick() - start) > 5) {
            return;                             // drop the byte; the RAM ring still has it
        }
    }
    uart_inst->TDR = (uint8_t) c;
}

// ---- Watchdog ----------------------------------------------------------------

int port_wdt_start(uint32_t id, uint32_t timeout_ms) {
    if (id != STUB_WDT_IWDG_ID || timeout_ms == 0) {
        return -1;
    }
    // LSI ~32 kHz; the smallest prescaler whose 12-bit reload covers the timeout.
    static const uint32_t prescalers[] = {
        IWDG_PRESCALER_4, IWDG_PRESCALER_8, IWDG_PRESCALER_16, IWDG_PRESCALER_32,
        IWDG_PRESCALER_64, IWDG_PRESCALER_128, IWDG_PRESCALER_256,
        #ifdef IWDG_PRESCALER_1024
        IWDG_PRESCALER_1024,                // the N6's IWDG divides further than the H7's
        #endif
    };
    static const uint32_t divs[] = { 4, 8, 16, 32, 64, 128, 256, 1024 };
    uint64_t ticks = (uint64_t) timeout_ms * LSI_VALUE / 1000;
    size_t i = 0;
    while (i + 1 < sizeof(prescalers) / sizeof(prescalers[0]) && ticks / divs[i] > 4096) {
        i++;
    }
    uint32_t reload = ticks / divs[i];
    if (reload == 0) {
        reload = 1;
    }
    if (reload > 4096) {
        reload = 4096;
    }
    IWDG_HandleTypeDef h = {
        .Instance = IWDG,
        .Init = {
            .Prescaler = prescalers[i],
            .Reload = reload - 1,
            .Window = IWDG_WINDOW_DISABLE,
            #if defined(STM32N6)
            .EWI = 0,
            #endif
        },
    };
    return (HAL_IWDG_Init(&h) == HAL_OK) ? 0 : -1;
}

// ---- Jump --------------------------------------------------------------------

#if defined(STM32H7)
// A buffered write's error lands at the next barrier; say which step issued it (the UART is
// still open here). Nothing after this point depends on those writes, so the boot goes on.
static void jump_step(uint32_t step) {
    static uint32_t seen = 0;
    __DSB();
    __ISB();
    if (flash_faults != seen) {
        seen = flash_faults;
        log_kv("stray fault at jump step", step);
    }
}
#else
static void jump_step(uint32_t step) {
    (void) step;
    __DSB();
}
#endif

void port_jump(uint32_t addr) {
    #if defined(STM32H7)
    jumping = true;
    #endif
    jump_step(0);
    port_flash_prog_end();
    jump_step(1);

    #if defined(STM32H7)
    // As the bootloader leaves the QSPI: the part soft-reset, the controller reset and
    // unclocked, its pins back to inputs. The firmware's driver brings it all up itself.
    qspi_flash_deinit();
    jump_step(2);
    for (size_t i = STUB_QSPI_PIN_START; i < STUB_QSPI_PIN_START + STUB_QSPI_PIN_COUNT; i++) {
        HAL_GPIO_DeInit(stub_pins[i].gpio, stub_pins[i].pin);
    }
    jump_step(3);
    #endif

    NVIC_DisableIRQ(SysTick_IRQn);
    NVIC_ClearPendingIRQ(SysTick_IRQn);
    SysTick->CTRL &= ~SysTick_CTRL_ENABLE_Msk;

    port_led(false);
    jump_step(4);

    // As the bootloader leaves things: caches off, the memory map live.
    SCB_CleanDCache();
    jump_step(5);
    SCB_DisableDCache();
    SCB_DisableICache();
    __DSB();
    __ISB();

    ((void (*)(void)) (*((uint32_t *) (addr + 4))))();
    while (1) {
    }
}

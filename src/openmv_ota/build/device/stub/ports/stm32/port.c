/*
 * OpenMV boot stub -- STM32 port (N6 first).
 *
 * The stub inherits the bootloader's clock tree and MPU map and leaves both alone,
 * exactly as the firmware does when the bootloader jumps to it. It owns only the
 * flash controller (through the bootloader's XSPI driver, copied here), the LED,
 * the debug UART and the watchdog.
 */
#include <string.h>
#include STM32_HAL_H
#include "stub.h"
#include "port.h"
#include "port_stm32.h"
#include "board_config.h"               // the firmware's board: oscillators, PLLs, ICs

uint32_t SystemCoreClock = HSI_VALUE;

static bool prog_mode = false;

void SystemCoreClockUpdate(void) {
    SystemCoreClock = HAL_RCC_GetCpuClockFreq();
}

void SysTick_Handler(void) {
    HAL_IncTick();
}

static void gpio_clock(GPIO_TypeDef *gpio) {
    if (gpio == GPIOA) { __HAL_RCC_GPIOA_CLK_ENABLE(); }
    if (gpio == GPIOB) { __HAL_RCC_GPIOB_CLK_ENABLE(); }
    if (gpio == GPIOC) { __HAL_RCC_GPIOC_CLK_ENABLE(); }
    if (gpio == GPIOD) { __HAL_RCC_GPIOD_CLK_ENABLE(); }
    if (gpio == GPIOE) { __HAL_RCC_GPIOE_CLK_ENABLE(); }
    if (gpio == GPIOF) { __HAL_RCC_GPIOF_CLK_ENABLE(); }
    if (gpio == GPIOG) { __HAL_RCC_GPIOG_CLK_ENABLE(); }
    if (gpio == GPIOH) { __HAL_RCC_GPIOH_CLK_ENABLE(); }
    if (gpio == GPION) { __HAL_RCC_GPION_CLK_ENABLE(); }
    if (gpio == GPIOO) { __HAL_RCC_GPIOO_CLK_ENABLE(); }
    if (gpio == GPIOP) { __HAL_RCC_GPIOP_CLK_ENABLE(); }
    if (gpio == GPIOQ) { __HAL_RCC_GPIOQ_CLK_ENABLE(); }
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
    uint32_t clk = HAL_RCCEx_GetPeriphCLKFreq(periph);
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
        IWDG_PRESCALER_64, IWDG_PRESCALER_128, IWDG_PRESCALER_256, IWDG_PRESCALER_1024,
    };
    static const uint32_t divs[] = { 4, 8, 16, 32, 64, 128, 256, 1024 };
    uint64_t ticks = (uint64_t) timeout_ms * LSI_VALUE / 1000;
    size_t i = 0;
    while (i + 1 < sizeof(divs) / sizeof(divs[0]) && ticks / divs[i] > 4096) {
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
            .EWI = 0,
        },
    };
    return (HAL_IWDG_Init(&h) == HAL_OK) ? 0 : -1;
}

// ---- Jump --------------------------------------------------------------------

void port_jump(uint32_t addr) {
    port_flash_prog_end();

    NVIC_DisableIRQ(SysTick_IRQn);
    NVIC_ClearPendingIRQ(SysTick_IRQn);
    SysTick->CTRL &= ~SysTick_CTRL_ENABLE_Msk;

    port_led(false);

    // As the bootloader leaves things: caches off, the memory map live.
    SCB_CleanDCache();
    SCB_DisableDCache();
    SCB_DisableICache();
    __DSB();
    __ISB();

    ((void (*)(void)) (*((uint32_t *) (addr + 4))))();
    while (1) {
    }
}

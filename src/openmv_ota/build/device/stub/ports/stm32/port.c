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

uint32_t SystemCoreClock = HSI_VALUE;

static bool prog_mode = false;
static UART_HandleTypeDef huart;
static bool uart_open = false;

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
    SCB_EnableDCache();
    return 0;
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
    // Out of the memory map and back to a known flash state: the driver resets the
    // controller and hardware-resets the part (octal DTR -> 1-line SPI).
    spi_flash_deinit();
    if (xspi_flash_init() != 0) {
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

static void uart_clock(USART_TypeDef *uart) {
    #ifdef USART1
    if (uart == USART1) { __HAL_RCC_USART1_CLK_ENABLE(); }
    #endif
    #ifdef USART2
    if (uart == USART2) { __HAL_RCC_USART2_CLK_ENABLE(); }
    #endif
    #ifdef USART3
    if (uart == USART3) { __HAL_RCC_USART3_CLK_ENABLE(); }
    #endif
    #ifdef UART4
    if (uart == UART4) { __HAL_RCC_UART4_CLK_ENABLE(); }
    #endif
    #ifdef UART5
    if (uart == UART5) { __HAL_RCC_UART5_CLK_ENABLE(); }
    #endif
    #ifdef USART6
    if (uart == USART6) { __HAL_RCC_USART6_CLK_ENABLE(); }
    #endif
    #ifdef UART7
    if (uart == UART7) { __HAL_RCC_UART7_CLK_ENABLE(); }
    #endif
    #ifdef UART8
    if (uart == UART8) { __HAL_RCC_UART8_CLK_ENABLE(); }
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
    gpio_clock(u->gpio);
    GPIO_InitTypeDef cfg = {
        .Pin = u->pin,
        .Mode = GPIO_MODE_AF_PP,
        .Pull = GPIO_PULLUP,
        .Speed = GPIO_SPEED_FREQ_LOW,
        .Alternate = u->alt,
    };
    HAL_GPIO_Init(u->gpio, &cfg);
    uart_clock(u->uart);

    memset(&huart, 0, sizeof(huart));
    huart.Instance = u->uart;
    huart.Init.BaudRate = baud;
    huart.Init.WordLength = UART_WORDLENGTH_8B;
    huart.Init.StopBits = UART_STOPBITS_1;
    huart.Init.Parity = UART_PARITY_NONE;
    huart.Init.Mode = UART_MODE_TX;
    huart.Init.HwFlowCtl = UART_HWCONTROL_NONE;
    huart.Init.OverSampling = UART_OVERSAMPLING_16;
    if (HAL_UART_Init(&huart) != HAL_OK) {
        return -1;
    }
    uart_open = true;
    return 0;
}

void port_uart_putc(char c) {
    if (uart_open) {
        HAL_UART_Transmit(&huart, (const uint8_t *) &c, 1, 10);
    }
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

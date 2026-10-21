/*
 * OpenMV boot stub -- OPENMV_N6 board configuration.
 *
 * Numbers the stub builds in. They describe the flash the bootloader (boot/) and
 * the firmware (boards/OPENMV_N6/board_config.h in the openmv repo) already agree
 * on; the stub takes the FIRMWARE partition's first erase block for itself and
 * installs into the rest.
 *
 * External octal flash, XIP at 0x70000000:
 *   BOOTLOADER  0x000000-0x080000
 *   FIRMWARE    0x080000-0x400000   stub @0x080000 (64 KiB) | install window | record
 *   FILESYSTEM  0x400000-0x800000
 *   ROMFS0      0x800000-0x2000000  two 12 MiB OTA slots
 */
#ifndef STUB_CONFIG_H
#define STUB_CONFIG_H

// Where the stub is linked (the address the bootloader jumps to) and the SRAM it
// copies itself into before it touches the flash controller.
#define STUB_FLASH_ORIGIN           0x70080000
#define STUB_FLASH_LENGTH           0x10000
#define STUB_CODE_ORIGIN            0x34100000      // AXI SRAM2
#define STUB_CODE_LENGTH            0x80000
#define STUB_RAM_ORIGIN             0x30000000      // DTCM: stack, data, bss
#define STUB_RAM_LENGTH             0x20000
#define STUB_BUF_ORIGIN             0x34000000      // AXI SRAM1: decoder + flash buffers
#define STUB_BUF_LENGTH             0x100000
#define STUB_STACK_SIZE             0x4000

// The flash, in the memory-mapped view.
#define STUB_XIP_BASE               0x70000000
#define STUB_FLASH_BLOCK            0x10000          // erase block
#define STUB_INSTALL_START          0x70090000      // dest window
#define STUB_INSTALL_END            0x703FF000
#define STUB_RECORD_ADDR            0x703FF000      // the install record's sector
#define STUB_ROMFS_ORIGIN           0x70800000
#define STUB_ROMFS_LENGTH           0x01800000
#define STUB_CONTROL_STRIDE         16

// Where firmware may put its stack (the vector-table plausibility check).
#define STUB_RAM_START              0x30000000
#define STUB_RAM_END                0x34400000

// XSPI flash (as the bootloader's boot_config.h).
#define OMV_BOOT_XSPI_INSTANCE      (2)
#define OMV_BOOT_XSPI_FREQUENCY     (200000000)
#define OMV_BOOT_XSPI_FLASH_SIZE    (0x2000000)
#define OMV_BOOT_XSPI_FLASH_RST_PIN (13)            // index in stub_pins
#define STUB_LED_PIN                (0)             // index in stub_pins

#ifndef LINKER_SCRIPT
#include "port_stm32.h"

static const stub_pin_t stub_pins[] = {
    { .gpio = GPIOA, .pin = GPIO_PIN_7,  .speed = GPIO_SPEED_FREQ_LOW,       .mode = GPIO_MODE_OUTPUT_PP, .pull = GPIO_PULLUP },
    { .gpio = GPION, .pin = GPIO_PIN_0,  .speed = GPIO_SPEED_FREQ_VERY_HIGH, .mode = GPIO_MODE_AF_PP, .alt = GPIO_AF9_XSPIM_P2 },
    { .gpio = GPION, .pin = GPIO_PIN_1,  .speed = GPIO_SPEED_FREQ_VERY_HIGH, .mode = GPIO_MODE_AF_PP, .alt = GPIO_AF9_XSPIM_P2 },
    { .gpio = GPION, .pin = GPIO_PIN_2,  .speed = GPIO_SPEED_FREQ_VERY_HIGH, .mode = GPIO_MODE_AF_PP, .alt = GPIO_AF9_XSPIM_P2 },
    { .gpio = GPION, .pin = GPIO_PIN_3,  .speed = GPIO_SPEED_FREQ_VERY_HIGH, .mode = GPIO_MODE_AF_PP, .alt = GPIO_AF9_XSPIM_P2 },
    { .gpio = GPION, .pin = GPIO_PIN_4,  .speed = GPIO_SPEED_FREQ_VERY_HIGH, .mode = GPIO_MODE_AF_PP, .alt = GPIO_AF9_XSPIM_P2 },
    { .gpio = GPION, .pin = GPIO_PIN_5,  .speed = GPIO_SPEED_FREQ_VERY_HIGH, .mode = GPIO_MODE_AF_PP, .alt = GPIO_AF9_XSPIM_P2 },
    { .gpio = GPION, .pin = GPIO_PIN_6,  .speed = GPIO_SPEED_FREQ_VERY_HIGH, .mode = GPIO_MODE_AF_PP, .alt = GPIO_AF9_XSPIM_P2 },
    { .gpio = GPION, .pin = GPIO_PIN_7,  .speed = GPIO_SPEED_FREQ_VERY_HIGH, .mode = GPIO_MODE_AF_PP, .alt = GPIO_AF9_XSPIM_P2 },
    { .gpio = GPION, .pin = GPIO_PIN_8,  .speed = GPIO_SPEED_FREQ_VERY_HIGH, .mode = GPIO_MODE_AF_PP, .alt = GPIO_AF9_XSPIM_P2 },
    { .gpio = GPION, .pin = GPIO_PIN_9,  .speed = GPIO_SPEED_FREQ_VERY_HIGH, .mode = GPIO_MODE_AF_PP, .alt = GPIO_AF9_XSPIM_P2 },
    { .gpio = GPION, .pin = GPIO_PIN_10, .speed = GPIO_SPEED_FREQ_VERY_HIGH, .mode = GPIO_MODE_AF_PP, .alt = GPIO_AF9_XSPIM_P2 },
    { .gpio = GPION, .pin = GPIO_PIN_11, .speed = GPIO_SPEED_FREQ_VERY_HIGH, .mode = GPIO_MODE_AF_PP, .alt = GPIO_AF9_XSPIM_P2 },
    { .gpio = GPION, .pin = GPIO_PIN_12, .speed = GPIO_SPEED_FREQ_LOW,       .mode = GPIO_MODE_OUTPUT_PP, .pull = GPIO_PULLUP },
};
#define STUB_PINS_COUNT             (sizeof(stub_pins) / sizeof(stub_pins[0]))

// Debug UARTs by the firmware's machine.UART id (TX only).
static const stub_uart_t stub_uarts[] = {
    { .id = 3, .uart = USART3, .gpio = GPIOB, .pin = GPIO_PIN_10, .alt = GPIO_AF7_USART3 },
    { .id = 4, .uart = UART4,  .gpio = GPIOA, .pin = GPIO_PIN_12, .alt = GPIO_AF8_UART4 },
    { .id = 7, .uart = UART7,  .gpio = GPIOE, .pin = GPIO_PIN_8,  .alt = GPIO_AF8_UART7 },
};
#define STUB_UARTS_COUNT            (sizeof(stub_uarts) / sizeof(stub_uarts[0]))

// Watchdogs by the firmware's machine.WDT id: 0 = IWDG.
#define STUB_WDT_IWDG_ID            (0)
#endif // LINKER_SCRIPT

#endif // STUB_CONFIG_H

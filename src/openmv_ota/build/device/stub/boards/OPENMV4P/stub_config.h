/*
 * OpenMV boot stub -- OPENMV4P board configuration.
 *
 * Numbers the stub builds in. They describe the flash the bootloader and the firmware
 * (boards/OPENMV4P/board_config.h in the openmv repo) already agree on; the stub takes the
 * FIRMWARE area's first erase sector for itself and installs into the rest.
 *
 * Internal flash, 2 MiB in 128 KiB sectors (two banks of eight):
 *   BOOTLOADER  0x08000000-0x08020000
 *   FFS         0x08020000-0x08040000
 *   FIRMWARE    0x08040000-0x08200000   stub @0x08040000 (128 KiB) | install window | record
 * The FFS sector (0x08020000, unused: the filesystem is on the QSPI part) is the firmware's
 * second text region, installed as a second component; the install window spans both.
 * QSPI flash, mapped by the firmware at 0x90000000:
 *   ROMFS0      0x91800000  two OTA slots
 */
#ifndef STUB_CONFIG_H
#define STUB_CONFIG_H

// Where the stub is linked (the address the bootloader jumps to) and the SRAM it
// copies itself into before it touches the flash controller.
#define STUB_FLASH_ORIGIN           0x08040000
#define STUB_FLASH_LENGTH           0x20000
#define STUB_CODE_ORIGIN            0x24000000      // AXI SRAM: code
#define STUB_CODE_LENGTH            0x10000
#define STUB_RAM_ORIGIN             0x20000000      // DTCM: stack, data, bss
#define STUB_RAM_LENGTH             0x8000
#define STUB_BUF_ORIGIN             0x24010000      // AXI SRAM: decoder + flash block buffers
#define STUB_BUF_LENGTH             0x70000
#define STUB_BUF2_ORIGIN            0x20008000      // DTCM: the small buffers
#define STUB_BUF2_LENGTH            0x18000
#define STUB_STACK_SIZE             0x4000
#define STUB_DCACHE_OFF             1               // flash is programmed under the reads

// The firmware area (internal flash) and the slots (QSPI, addressed as the firmware maps them).
#define STUB_FLASH_BLOCK            0x20000         // erase sector of the install window
#define STUB_WRITE_UNIT             32              // one flash word
#define STUB_INSTALL_START          0x08020000      // dest window: the FFS sector, the stub's
#define STUB_INSTALL_END            0x081FF000      //   sector (refused), the firmware area
#define STUB_FIRMWARE_ADDR          0x08060000      // the firmware's vector table
#define STUB_RECORD_ADDR            0x081FF000      // the last 4 KiB of the last sector
#define STUB_ROMFS_ORIGIN           0x91800000
#define STUB_ROMFS_LENGTH           0x00800000
#define STUB_CONTROL_STRIDE         16
#define STUB_QSPI_BASE              0x90000000
#define STUB_QSPI_FLASH_SIZE        0x2000000

// Where firmware may put its stack (the vector-table plausibility check): DTCM, AXI
// SRAM, the D2/D3 SRAMs.
#define STUB_RAM_START              0x20000000
#define STUB_RAM_END                0x38010000

#define STUB_LED_PIN                (0)             // index in stub_pins, active low
#define STUB_QSPI_PIN_START         (1)             // stub_pins[1..6]: CS CLK D0 D1 D2 D3
#define STUB_QSPI_PIN_COUNT         (6)

#ifndef LINKER_SCRIPT
#include "port_stm32.h"

static const stub_pin_t stub_pins[] = {
    { .gpio = GPIOC, .pin = GPIO_PIN_0, .speed = GPIO_SPEED_FREQ_LOW, .mode = GPIO_MODE_OUTPUT_PP, .pull = GPIO_PULLUP },
    { .gpio = GPIOG, .pin = GPIO_PIN_6, .speed = GPIO_SPEED_FREQ_VERY_HIGH, .mode = GPIO_MODE_AF_PP, .alt = GPIO_AF10_QUADSPI },   // CS
    { .gpio = GPIOF, .pin = GPIO_PIN_10, .speed = GPIO_SPEED_FREQ_VERY_HIGH, .mode = GPIO_MODE_AF_PP, .alt = GPIO_AF9_QUADSPI },   // CLK
    { .gpio = GPIOF, .pin = GPIO_PIN_8, .speed = GPIO_SPEED_FREQ_VERY_HIGH, .mode = GPIO_MODE_AF_PP, .alt = GPIO_AF10_QUADSPI },   // D0
    { .gpio = GPIOF, .pin = GPIO_PIN_9, .speed = GPIO_SPEED_FREQ_VERY_HIGH, .mode = GPIO_MODE_AF_PP, .alt = GPIO_AF10_QUADSPI },   // D1
    { .gpio = GPIOF, .pin = GPIO_PIN_7, .speed = GPIO_SPEED_FREQ_VERY_HIGH, .mode = GPIO_MODE_AF_PP, .alt = GPIO_AF9_QUADSPI },   // D2
    { .gpio = GPIOF, .pin = GPIO_PIN_6, .speed = GPIO_SPEED_FREQ_VERY_HIGH, .mode = GPIO_MODE_AF_PP, .alt = GPIO_AF9_QUADSPI },   // D3
};
#define STUB_PINS_COUNT             (sizeof(stub_pins) / sizeof(stub_pins[0]))

// Debug UARTs by the firmware's machine.UART id (TX only), on the kernel clock the
// firmware leaves them on (the APB clock).
static const stub_uart_t stub_uarts[] = {
    { .id = 3, .uart = USART3, .gpio = GPIOB, .pin = GPIO_PIN_10, .alt = GPIO_AF7_USART3, .clk = LL_RCC_USART234578_CLKSOURCE_PCLK1 },
    { .id = 1, .uart = USART1, .gpio = GPIOB, .pin = GPIO_PIN_14, .alt = GPIO_AF4_USART1, .clk = LL_RCC_USART16_CLKSOURCE_PCLK2 },
};
#define STUB_UARTS_COUNT            (sizeof(stub_uarts) / sizeof(stub_uarts[0]))

// Watchdogs by the firmware's machine.WDT id: 0 = IWDG.
#define STUB_WDT_IWDG_ID            (0)
#endif // LINKER_SCRIPT

#endif // STUB_CONFIG_H

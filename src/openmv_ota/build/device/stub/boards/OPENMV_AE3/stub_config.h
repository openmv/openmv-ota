/*
 * OpenMV boot stub -- OPENMV_AE3 board configuration (Alif Ensemble E3, HP core).
 *
 * Numbers the stub builds in. They describe the flash the bootloader (boot/) and the
 * firmware (boards/OPENMV_AE3/board_config.h in the openmv repo) already agree on; the
 * stub takes the first 32 KiB of the HP partition for itself and installs into the rest
 * of the HP partition and the whole HE partition.
 *
 * On-chip MRAM (5.5 MiB, memory-mapped, 16-byte write lines, no erase):
 *   BOOT     0x80000000-0x80020000   bootloader
 *   HP       0x80020000-0x80320000   stub @0x80020000 (32 KiB) | HP firmware (install window)
 *   HE       0x80320000-0x8047E000   HE firmware (install window) | record @0x8047D000
 *   ROMFS1   0x8047E000-0x8057E000   the HE core's romfs (the main core writes it)
 *   TOC      0x8057E000-0x80580000
 * OSPI flash (32 MiB, the firmware maps it at 0xA0000000):
 *   RWFS     0x000000-0x800000       /flash
 *   ROMFS0   0x800000-0x2000000      two 12 MiB OTA slots
 */
#ifndef STUB_CONFIG_H
#define STUB_CONFIG_H

// Where the stub is linked (the address the bootloader jumps to) and the SRAM it copies
// itself into. MRAM executes in place, but the stub programs MRAM, so it runs elsewhere.
#define STUB_FLASH_ORIGIN           0x80020000
#define STUB_FLASH_LENGTH           0x8000
#define STUB_CODE_ORIGIN            0x02000000      // SRAM0
#define STUB_CODE_LENGTH            0x40000
#define STUB_RAM_ORIGIN             0x20000000      // DTCM: stack, data, bss
#define STUB_RAM_LENGTH             0x40000
#define STUB_BUF_ORIGIN             0x08000000      // SRAM1: decoder + flash buffers
#define STUB_BUF_LENGTH             0x100000
#define STUB_STACK_SIZE             0x4000

// The firmware, in the memory-mapped view. MRAM has no erase blocks: STUB_FLASH_BLOCK is
// the granularity the installer compares and programs at, and an "erase" writes 0xFF.
#define STUB_MRAM_START             0x80000000
#define STUB_MRAM_END               0x80580000
#define STUB_FLASH_BLOCK            0x1000
#define STUB_INSTALL_START          0x80028000      // dest window: the HP and HE partitions
#define STUB_INSTALL_END            0x8047D000
#define STUB_RECORD_ADDR            0x8047D000      // the install record's 4 KiB
#define STUB_CONTROL_STRIDE         16

// The slots, in the firmware's memory-mapped view of the OSPI part (the stub reads them
// through the controller instead: addr - STUB_OSPI_XIP_BASE on the chip).
#define STUB_OSPI_XIP_BASE          0xA0000000
#define STUB_OSPI_SIZE              0x02000000
#define STUB_ROMFS_ORIGIN           0xA0800000
#define STUB_ROMFS_LENGTH           0x01800000

// Where firmware may put its stack (the vector-table plausibility check): the HP core's
// stack is in its ITCM at 0, its data in DTCM/SRAM.
#define STUB_RAM_START              0x00000000
#define STUB_RAM_END                0x20100000

// Clocks the bootloader's run profile leaves: the HP core and its AXI at 400 MHz, the
// peripheral clock the UARTs take at 100 MHz.
#define STUB_CPU_CLOCK              (400000000)
#define STUB_AXI_CLOCK              (400000000)
#define STUB_UART_CLOCK             (100000000)

// OSPI flash (as the bootloader's boot_config.h): 1-line SPI at 50 MHz.
#define STUB_OSPI_SSI_BASE          (OSPI0_BASE)
#define STUB_OSPI_AES_BASE          (AES0_BASE)
#define STUB_OSPI_CLOCK             (50000000)
#define STUB_OSPI_RX_DELAY          (4)
#define STUB_OSPI_DDR_EDGE          (0)
#define STUB_OSPI_RXDS_DELAY        (12)
#define STUB_OSPI_SER               (1)
#define STUB_LED_PIN                (0)             // index in stub_pins
#define STUB_OSPI_RST_PIN           (1)
#define STUB_OSPI_PIN_START         (2)             // D0..D7, RXD, CK, CS follow

#ifndef LINKER_SCRIPT
#include "port_alif.h"

#define ALIF_GPIO(x)                ((GPIO_Type *) x##_BASE)
#define STUB_PAD_IO                 (PADCTRL_OUTPUT_DRIVE_STRENGTH_12MA)
#define STUB_PAD_Dx                 (PADCTRL_OUTPUT_DRIVE_STRENGTH_12MA | PADCTRL_SLEW_RATE_FAST | PADCTRL_READ_ENABLE)
#define STUB_PAD_D2                 (STUB_PAD_Dx | PADCTRL_DRIVER_DISABLED_PULL_UP)
#define STUB_PAD_Cx                 (PADCTRL_OUTPUT_DRIVE_STRENGTH_12MA | PADCTRL_SLEW_RATE_FAST)

static const stub_pin_t stub_pins[] = {
    { .gpio = ALIF_GPIO(GPIO6), .port = PORT_6, .pin = PIN_3, .alt = PINMUX_ALTERNATE_FUNCTION_0, .pad = STUB_PAD_IO },  // LED
    { .gpio = ALIF_GPIO(GPIO3), .port = PORT_3, .pin = PIN_3, .alt = PINMUX_ALTERNATE_FUNCTION_0, .pad = STUB_PAD_IO },  // OSPI RST
    { .gpio = ALIF_GPIO(GPIO2), .port = PORT_2, .pin = PIN_0, .alt = PINMUX_ALTERNATE_FUNCTION_1, .pad = STUB_PAD_Dx },  // D0
    { .gpio = ALIF_GPIO(GPIO2), .port = PORT_2, .pin = PIN_1, .alt = PINMUX_ALTERNATE_FUNCTION_1, .pad = STUB_PAD_Dx },  // D1
    { .gpio = ALIF_GPIO(GPIO2), .port = PORT_2, .pin = PIN_2, .alt = PINMUX_ALTERNATE_FUNCTION_1, .pad = STUB_PAD_D2 },  // D2
    { .gpio = ALIF_GPIO(GPIO2), .port = PORT_2, .pin = PIN_3, .alt = PINMUX_ALTERNATE_FUNCTION_1, .pad = STUB_PAD_Dx },  // D3
    { .gpio = ALIF_GPIO(GPIO2), .port = PORT_2, .pin = PIN_4, .alt = PINMUX_ALTERNATE_FUNCTION_1, .pad = STUB_PAD_Dx },  // D4
    { .gpio = ALIF_GPIO(GPIO2), .port = PORT_2, .pin = PIN_5, .alt = PINMUX_ALTERNATE_FUNCTION_1, .pad = STUB_PAD_Dx },  // D5
    { .gpio = ALIF_GPIO(GPIO2), .port = PORT_2, .pin = PIN_6, .alt = PINMUX_ALTERNATE_FUNCTION_1, .pad = STUB_PAD_Dx },  // D6
    { .gpio = ALIF_GPIO(GPIO2), .port = PORT_2, .pin = PIN_7, .alt = PINMUX_ALTERNATE_FUNCTION_1, .pad = STUB_PAD_Dx },  // D7
    { .gpio = ALIF_GPIO(GPIO1), .port = PORT_1, .pin = PIN_6, .alt = PINMUX_ALTERNATE_FUNCTION_1, .pad = STUB_PAD_Dx },  // RXD
    { .gpio = ALIF_GPIO(GPIO3), .port = PORT_3, .pin = PIN_0, .alt = PINMUX_ALTERNATE_FUNCTION_1, .pad = STUB_PAD_Cx },  // CK
    { .gpio = ALIF_GPIO(GPIO3), .port = PORT_3, .pin = PIN_2, .alt = PINMUX_ALTERNATE_FUNCTION_1, .pad = STUB_PAD_Cx },  // CS
};
#define STUB_PINS_COUNT             (sizeof(stub_pins) / sizeof(stub_pins[0]))

// Debug UARTs by the firmware's machine.UART id (TX only; the pin functions are the
// firmware's, ports/alif/alif_pins.h in the openmv repo).
static const stub_uart_t stub_uarts[] = {
    { .id = 1, .uart = (UART_Type *) UART1_BASE, .port = PORT_0, .pin = PIN_5, .alt = PINMUX_ALTERNATE_FUNCTION_2 },
    { .id = 3, .uart = (UART_Type *) UART3_BASE, .port = PORT_1, .pin = PIN_3, .alt = PINMUX_ALTERNATE_FUNCTION_1 },
    { .id = 4, .uart = (UART_Type *) UART4_BASE, .port = PORT_5, .pin = PIN_1, .alt = PINMUX_ALTERNATE_FUNCTION_2 },
    { .id = 5, .uart = (UART_Type *) UART5_BASE, .port = PORT_5, .pin = PIN_3, .alt = PINMUX_ALTERNATE_FUNCTION_2 },
};
#define STUB_UARTS_COUNT            (sizeof(stub_uarts) / sizeof(stub_uarts[0]))

// Watchdogs by the firmware's machine.WDT id: 0 = the HP core's.
#define STUB_WDT_HP_ID              (0)
#endif // LINKER_SCRIPT

#endif // STUB_CONFIG_H

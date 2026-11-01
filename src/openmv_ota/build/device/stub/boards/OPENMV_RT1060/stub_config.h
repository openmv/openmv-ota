/*
 * OpenMV boot stub -- OPENMV_RT1060 (i.MX RT1062) layout.
 *
 * One 16 MiB QSPI NOR part on FlexSPI, memory-mapped at 0x60000000 and booted by the chip's
 * ROM through the FCB at its start. The ROM runs NXP's MCU bootloader from 0x60001000 (the
 * "SBL" the OTA tool provisions), and that jumps to the vector table at 0x60042000: the
 * FLASH_ISR slot of a firmware image laid out from 0x60040000 (FCB, IVT, vectors, text).
 * The stub takes that image's place -- 0x60040000..0x6004FFFF, its head at +0x2000 where the
 * bootloader looks, the 8 KiB before it blank -- and the firmware moves up to 0x60050000 with
 * the same internal layout (its vectors at 0x60052000). The slots are on the same part.
 */
#ifndef STUB_CONFIG_H
#define STUB_CONFIG_H

// Where the stub is linked and the RAM it copies itself into. OCRAM2 (0x20200000, 512 KiB)
// is a fixed bank; the FlexRAM banks (ITCM/DTCM/OCRAM at 0x20280000) are whatever the
// bootloader left and the firmware repartitions them at its own reset, so the stub stays out.
#define STUB_FLASH_ORIGIN           0x60040000
#define STUB_FLASH_LENGTH           0x10000
#define STUB_FLASH_HEAD_OFFSET      0x2000          // the bootloader's jump target, in the region
#define STUB_CODE_ORIGIN            0x20200000      // OCRAM2: code
#define STUB_CODE_LENGTH            0x10000
#define STUB_RAM_ORIGIN             0x20210000      // OCRAM2: stack, data, bss
#define STUB_RAM_LENGTH             0x10000
#define STUB_BUF_ORIGIN             0x20220000      // OCRAM2: decoder + flash buffers
#define STUB_BUF_LENGTH             0x60000
#define STUB_STACK_SIZE             0x4000

// The flash, in the memory-mapped (FlexSPI AHB) view.
#define STUB_XIP_BASE               0x60000000
#define STUB_FLASH_SIZE             0x01000000
#define STUB_FLASH_BLOCK            0x1000          // erase sector
#define STUB_FLASH_PAGE             0x100           // program page
#define STUB_INSTALL_START          0x60050000      // dest window
#define STUB_INSTALL_END            0x603FF000
#define STUB_RECORD_ADDR            0x603FF000      // the install record's sector
#define STUB_FIRMWARE_ADDR          (STUB_INSTALL_START + 0x2000)   // the firmware's FLASH_ISR
#define STUB_ROMFS_ORIGIN           0x60800000
#define STUB_ROMFS_LENGTH           0x00800000
#define STUB_CONTROL_STRIDE         16

// Where firmware may put its stack (the vector-table plausibility check): ITCM, DTCM, OCRAM.
#define STUB_RAM_START              0x00001000
#define STUB_RAM_END                0x20300000

#define STUB_FLEXSPI_INSTANCE       (0)
#define STUB_UART_CLOCK             (24000000)      // the oscillator, selected by port_uart_init
#define STUB_LED_PIN                (0)             // index in stub_pins
#define STUB_WDT_WDOG1_ID           (0)             // machine.WDT(0) is WDOG1

#ifndef LINKER_SCRIPT
#include "port_mimxrt.h"

static const stub_pin_t stub_pins[] = {
    { .gpio = GPIO2, .pin = 10, .mux = kIOMUXC_SW_MUX_CTL_PAD_GPIO_B0_10, .alt = 5 },   // LED1 (active low)
};
#define STUB_PINS_COUNT             (sizeof(stub_pins) / sizeof(stub_pins[0]))

// Debug UARTs by the firmware's machine.UART id (TX only): 1 = LPUART1 on P4, 0 = LPUART3.
static const stub_uart_t stub_uarts[] = {
    { .id = 1, .uart = LPUART1, .mux = kIOMUXC_SW_MUX_CTL_PAD_GPIO_AD_B0_12, .alt = 2,
      .ccgr = &CCM->CCGR5, .ccgr_mask = CCM_CCGR5_CG12_MASK },
    { .id = 0, .uart = LPUART3, .mux = kIOMUXC_SW_MUX_CTL_PAD_GPIO_B0_08, .alt = 2,
      .ccgr = &CCM->CCGR0, .ccgr_mask = CCM_CCGR0_CG6_MASK },
};
#define STUB_UARTS_COUNT            (sizeof(stub_uarts) / sizeof(stub_uarts[0]))
#endif // LINKER_SCRIPT

#endif // STUB_CONFIG_H

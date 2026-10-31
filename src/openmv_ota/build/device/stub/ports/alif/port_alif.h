/*
 * OpenMV boot stub -- Alif Ensemble port types shared with the board config.
 */
#ifndef STUB_PORT_ALIF_H
#define STUB_PORT_ALIF_H

#include <stdint.h>
#include ALIF_CMSIS_H
#include "gpio.h"
#include "pinconf.h"
#include "uart.h"

typedef struct {
    GPIO_Type *gpio;
    uint8_t port;               // pinconf port / pin
    uint8_t pin;
    uint8_t alt;                // PINMUX_ALTERNATE_FUNCTION_x
    uint8_t pad;                // PADCTRL_* bits
} stub_pin_t;

typedef struct {
    uint32_t id;                // machine.UART id (== the Alif UART instance)
    UART_Type *uart;
    uint8_t port;               // TX pin
    uint8_t pin;
    uint8_t alt;
} stub_uart_t;

// Used by the flash driver (alif_ospi.c).
void port_pin_write(uint32_t index, uint32_t state);
void port_delay_ms(uint32_t ms);

// The head's prep hook (start.s calls it before copying the image to SRAM).
void stub_head_prep(void);

// alif_ospi.c: the slots' OSPI part, 1-line commands, addresses relative to the chip.
int ospi_flash_init(void);
int ospi_flash_deinit(void);
int ospi_flash_read(uint32_t addr, uint8_t *buf, uint32_t size);
int ospi_flash_write(uint32_t addr, const uint8_t *buf, uint32_t size);

#endif // STUB_PORT_ALIF_H

/*
 * OpenMV boot stub -- i.MX RT port: the board table's types.
 */
#ifndef STUB_PORT_MIMXRT_H
#define STUB_PORT_MIMXRT_H
#include <stdint.h>
#include "fsl_device_registers.h"

typedef struct {
    GPIO_Type *gpio;
    uint32_t pin;                               // bit in the GPIO module
    uint32_t mux;                               // kIOMUXC_SW_MUX_CTL_PAD_* index
    uint32_t alt;                               // mux mode
} stub_pin_t;

typedef struct {
    uint32_t id;                                // machine.UART id
    LPUART_Type *uart;
    uint32_t mux;                               // the TX pad
    uint32_t alt;
    volatile uint32_t *ccgr;                    // the module's clock gate
    uint32_t ccgr_mask;
} stub_uart_t;

#endif // STUB_PORT_MIMXRT_H

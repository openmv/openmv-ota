/*
 * OpenMV boot stub -- STM32 port types shared with the board config.
 */
#ifndef STUB_PORT_STM32_H
#define STUB_PORT_STM32_H

#include <stdint.h>
#include STM32_HAL_H

typedef struct {
    GPIO_TypeDef *gpio;
    uint16_t pin;
    uint16_t alt;
    uint16_t mode;
    uint16_t pull;
    uint16_t speed;
} stub_pin_t;

typedef struct {
    uint32_t id;                // machine.UART id
    USART_TypeDef *uart;
    GPIO_TypeDef *gpio;         // TX pin
    uint16_t pin;
    uint16_t alt;
    uint32_t clk;               // kernel clock selector (an LL_RCC_*_CLKSOURCE_*)
} stub_uart_t;

// Used by the copied bootloader flash driver (xspi.c).
void port_pin_write(uint32_t index, uint32_t state);
void port_delay_ms(uint32_t ms);

int xspi_flash_init(void);
int spi_flash_memory_map(bool dtr);
int spi_flash_deinit(void);
int spi_flash_memory_unmap(void);
bool spi_flash_initialized(void);
int spi_flash_read(uint32_t addr, uint8_t *buf, uint32_t size);
int spi_flash_write(uint32_t addr, const uint8_t *buf, uint32_t size);
int spi_flash_erase_block(uint32_t addr);

#endif // STUB_PORT_STM32_H

/*
 * OpenMV boot stub -- what a port provides.
 *
 * Flash addresses are absolute (the memory-mapped/XIP view). A port starts in
 * "mapped" mode, where port_flash_read() is a copy out of the memory map and the
 * flash controller is untouched. port_flash_prog_begin() switches to indirect
 * (programming) mode; reads still work, and erase/write become available.
 * port_flash_prog_end() returns to the memory map and drops any cached view.
 */
#ifndef STUB_PORT_H
#define STUB_PORT_H

#include <stdint.h>
#include <stdbool.h>

int port_init(void);
void port_jump(uint32_t addr) __attribute__((noreturn));
uint32_t port_ticks_ms(void);
void port_led(bool on);
void port_led_blink(uint32_t interval_ms);
void port_cache_clean(const void *p, uint32_t n);   // write back dirty cache lines

int port_flash_read(uint32_t addr, uint8_t *buf, uint32_t size);
int port_flash_prog_begin(void);
int port_flash_prog_end(void);
int port_flash_erase(uint32_t addr);                                 // one STUB_FLASH_BLOCK
int port_flash_write(uint32_t addr, const uint8_t *buf, uint32_t size);   // 16-byte units, no erase

int port_uart_init(uint32_t id, uint32_t baud);                     // -1: unknown id
void port_uart_putc(char c);
int port_wdt_start(uint32_t id, uint32_t timeout_ms);               // -1: unknown id

#endif // STUB_PORT_H

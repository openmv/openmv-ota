/*
 * OpenMV boot stub -- debug log over the port UART (none open = silent).
 */
#include "stub.h"
#include "port.h"

static bool log_on = false;

// A RAM copy of everything logged, UART or not: `magic, len` then the text, at a fixed
// address a debugger can dump after the fact (the bench reads it over SWD).
#define RING_MAGIC  (0x474C5453U)   // "STLG"
#define RING_SIZE   (16384 - 8)
typedef struct {
    uint32_t magic;
    uint32_t len;
    char text[RING_SIZE];
} log_ring_t;
static log_ring_t ring STUB_BUFFER_SMALL;

static void ring_putc(char c) {
    if (ring.magic != RING_MAGIC) {
        ring.magic = RING_MAGIC;
        ring.len = 0;
    }
    if (ring.len == RING_SIZE) {
        // Full: drop the oldest half so the latest boots always survive.
        memmove(ring.text, ring.text + RING_SIZE / 2, RING_SIZE / 2);
        ring.len = RING_SIZE / 2;
    }
    ring.text[ring.len++] = c;
}

void log_open(uint32_t uart_id, uint32_t baud) {
    log_on = (port_uart_init(uart_id, baud) == 0);
}

void log_str(const char *s) {
    while (*s) {
        ring_putc(*s);
        if (log_on) {
            port_uart_putc(*s);
        }
        s++;
    }
    port_cache_clean(&ring, sizeof(ring));      // a reset must not lose the tail
}

void log_hex(uint32_t v) {
    static const char digits[] = "0123456789abcdef";
    char buf[11] = "0x";
    for (int i = 0; i < 8; i++) {
        buf[2 + i] = digits[(v >> (28 - 4 * i)) & 0xF];
    }
    buf[10] = 0;
    log_str(buf);
}

void log_dec(uint32_t v) {
    char buf[11];
    int i = 10;
    buf[i] = 0;
    do {
        buf[--i] = '0' + (v % 10);
        v /= 10;
    } while (v && i);
    log_str(&buf[i]);
}

void log_line(const char *s) {
    log_str("stub: ");
    log_str(s);
    log_str("\r\n");
}

void log_kv(const char *k, uint32_t v) {
    log_str("stub: ");
    log_str(k);
    log_str(" ");
    log_hex(v);
    log_str("\r\n");
}

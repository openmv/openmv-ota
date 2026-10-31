/*
 * OpenMV boot stub -- the slots' OSPI part on the Alif Ensemble, after the bootloader's
 * driver (boot/src/ports/alif/alif_ospi.c in the openmv repo).
 *
 * The part is hardware-reset into 1-line SPI and driven through the controller's FIFO:
 * READ 0x13 and PAGE PROGRAM 0x12 with 4-byte addresses, no dummy cycles, 50 MHz. The
 * firmware runs the part in octal DDR through the XIP window, so the stub never uses that
 * window: every slot access here is a command. The slots are never erased by the stub
 * (status marks are 16-byte programs over erased bytes), so there is no erase.
 */
#include "stub.h"
#include "port.h"
#include "port_alif.h"
#include "ospi_private.h"

#define OSPI_INST_8BIT      (0x02U)
#define OSPI_DFS_8BIT       (0x07U)
#define OSPI_FRF_SINGLE     (0x00U)
#define OSPI_ADDR_0BIT      (0x00U)
#define OSPI_ADDR_32BIT     (0x08U)

#define OSPI_FIFO_SIZE      (128)
#define OSPI_PAGE_SIZE      (128)
#define OSPI_POLL_TIMEOUT   (1000000)

#define CMD_RDSR            (0x05)
#define CMD_RDID            (0x9F)
#define CMD_READ            (0x13)
#define CMD_WREN            (0x06)
#define CMD_WRITE           (0x12)

#define SR_WIP              (1 << 0)
#define SR_WEL              (1 << 1)

typedef struct {
    ssi_regs_t *regs;
    aes_regs_t *aes_regs;
    bool initialized;
} ospi_flash_t;

static ospi_flash_t ospi;

static void ospi_setup(ospi_flash_t *o, uint32_t addr_len, uint32_t ndf, uint32_t wait_cycles) {
    ospi_writel(o, ser, 0);
    spi_disable(o);

    uint32_t val = CTRLR0_IS_MST
                   | (OSPI_DFS_8BIT << CTRLR0_DFS_OFFSET)
                   | (OSPI_FRF_SINGLE << CTRLR0_SPI_FRF_OFFSET)
                   | ((ndf ? SPI_TMOD_TR : TMOD_TO) << CTRLR0_TMOD_OFFSET);
    ospi_writel(o, ctrlr0, val);
    ospi_writel(o, ctrlr1, (ndf ? (ndf - 1) : 0));

    val = TRANS_TYPE_FRF_DEFINED
          | (1 << CTRLR0_SPI_RXDS_EN_OFFSET)
          | (addr_len << CTRLR0_ADDR_L_OFFSET)
          | (OSPI_INST_8BIT << CTRLR0_INST_L_OFFSET)
          | (wait_cycles << CTRLR0_WAIT_CYCLES_OFFSET);
    ospi_writel(o, spi_ctrlr0, val);
    spi_enable(o);
}

static inline void ospi_push(ospi_flash_t *o, uint32_t data) {
    ospi_writel(o, data_reg, data);
}

static int ospi_wait_idle(ospi_flash_t *o) {
    uint32_t timeout = OSPI_POLL_TIMEOUT;
    while ((ospi_readl(o, sr) & (SR_TF_EMPTY | SR_BUSY)) != SR_TF_EMPTY) {
        if (--timeout == 0) {
            return -1;
        }
    }
    return 0;
}

static void push_addr(ospi_flash_t *o, uint32_t addr_len, uint32_t addr) {
    // 0, 24 or 32 bits, most significant byte first.
    for (uint32_t i = 0; i < addr_len / 2; i++) {
        ospi_push(o, addr >> ((addr_len / 2 - 1) * 8 - i * 8));
    }
}

static int ospi_send(ospi_flash_t *o, uint32_t cmd, uint32_t addr_len, uint32_t addr,
                     uint32_t data_len, const uint8_t *data) {
    ospi_setup(o, addr_len, 0, 0);
    ospi_push(o, cmd);
    push_addr(o, addr_len, addr);
    for (uint32_t i = 0; i < data_len; i++) {
        ospi_push(o, data[i]);
    }
    ospi_writel(o, ser, STUB_OSPI_SER);
    return ospi_wait_idle(o);
}

static int ospi_recv(ospi_flash_t *o, uint32_t cmd, uint32_t addr_len, uint32_t addr,
                     uint32_t data_len, uint8_t *data) {
    ospi_setup(o, addr_len, data_len, 0);
    ospi_push(o, cmd);
    push_addr(o, addr_len, addr);
    for (uint32_t i = 0; i < data_len; i++) {
        ospi_push(o, 0x00);                     // one dummy frame per data byte
    }
    ospi_writel(o, ser, STUB_OSPI_SER);

    // The command and address come back as high-Z frames first.
    uint32_t skip = addr_len / 2 + 1;
    for (uint32_t i = 0; i < skip + data_len; i++) {
        uint32_t timeout = 100000;
        while (ospi_readl(o, rxflr) == 0) {
            if (--timeout == 0) {
                return -1;
            }
        }
        uint32_t v = ospi_readl(o, data_reg);
        if (i >= skip) {
            data[i - skip] = v;
        }
    }
    return 0;                                   // every frame is in: the transfer is over
}

static int ospi_wait_sr(ospi_flash_t *o, uint8_t mask, uint8_t val) {
    uint32_t start = port_ticks_ms();
    do {
        uint8_t sr;
        if (ospi_recv(o, CMD_RDSR, OSPI_ADDR_0BIT, 0, 1, &sr) != 0) {
            return -1;
        }
        if ((sr & mask) == val) {
            return 0;
        }
    } while ((port_ticks_ms() - start) < 1000);
    return -1;
}

static int ospi_write_enable(ospi_flash_t *o) {
    if (ospi_send(o, CMD_WREN, OSPI_ADDR_0BIT, 0, 0, NULL) != 0) {
        return -1;
    }
    return ospi_wait_sr(o, SR_WEL, SR_WEL);
}

static void ospi_part_reset(void) {
    // RESET# low, then high: back to 1-line SPI, whatever mode the firmware or the
    // bootloader left the part in.
    port_pin_write(STUB_OSPI_RST_PIN, 0);
    port_delay_ms(1);
    port_pin_write(STUB_OSPI_RST_PIN, 1);
    port_delay_ms(1);
}

int ospi_flash_init(void) {
    ospi_flash_t *o = &ospi;
    if (o->initialized) {
        return 0;
    }
    ospi_part_reset();

    o->regs = (ssi_regs_t *) STUB_OSPI_SSI_BASE;
    o->aes_regs = (aes_regs_t *) STUB_OSPI_AES_BASE;

    spi_disable(o);
    ospi_writel(o, ser, 0);
    ospi_writel(o, rx_sample_dly, STUB_OSPI_RX_DELAY);
    ospi_writel(o, txd_drive_edge, STUB_OSPI_DDR_EDGE);
    o->aes_regs->aes_control &= ~AES_CONTROL_XIP_EN;
    o->aes_regs->aes_rxds_delay = STUB_OSPI_RXDS_DELAY;
    spi_set_clk(o, STUB_AXI_CLOCK / STUB_OSPI_CLOCK);
    spi_enable(o);

    uint8_t id[3] = { 0 };
    if (ospi_recv(o, CMD_RDID, OSPI_ADDR_0BIT, 0, 3, id) != 0) {
        log_line("ospi id timeout");
        return -1;
    }
    uint32_t jedec = ((uint32_t) id[0] << 16) | ((uint32_t) id[1] << 8) | id[2];
    log_kv("ospi jedec", jedec);
    if (jedec == 0 || jedec == 0xFFFFFF) {
        log_line("ospi no part");
        return -1;
    }
    o->initialized = true;
    return 0;
}

int ospi_flash_deinit(void) {
    ospi_flash_t *o = &ospi;
    if (o->regs) {
        spi_disable(o);
        spi_set_clk(o, 0);
    }
    o->initialized = false;
    ospi_part_reset();                          // as the bootloader leaves it
    return 0;
}

int ospi_flash_read(uint32_t addr, uint8_t *buf, uint32_t size) {
    ospi_flash_t *o = &ospi;
    if (!o->initialized) {
        return -1;
    }
    while (size) {
        uint32_t n = (size > OSPI_FIFO_SIZE) ? OSPI_FIFO_SIZE : size;
        if (ospi_recv(o, CMD_READ, OSPI_ADDR_32BIT, addr, n, buf) != 0) {
            return -1;
        }
        addr += n;
        buf += n;
        size -= n;
    }
    return 0;
}

int ospi_flash_write(uint32_t addr, const uint8_t *buf, uint32_t size) {
    ospi_flash_t *o = &ospi;
    if (!o->initialized) {
        return -1;
    }
    while (size) {
        // Whole programs inside one page: the FIFO holds a half page.
        uint32_t n = OSPI_PAGE_SIZE - (addr % OSPI_PAGE_SIZE);
        if (n > size) {
            n = size;
        }
        if (ospi_write_enable(o) != 0 ||
            ospi_send(o, CMD_WRITE, OSPI_ADDR_32BIT, addr, n, buf) != 0 ||
            ospi_wait_sr(o, SR_WIP, 0) != 0) {
            return -1;
        }
        addr += n;
        buf += n;
        size -= n;
    }
    return 0;
}

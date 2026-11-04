/*
 * OpenMV boot stub -- STM32H7 QSPI flash (the ROMFS slots).
 *
 * The H7 boards keep their OTA slots on a QSPI part the firmware memory-maps at
 * 0x90000000; the bootloader leaves the controller reset and the part in its power-on
 * state, and so does this driver when the stub jumps. Everything here is 1-line SPI at a
 * clock the slowest command allows: the stub reads two 8 KiB slot tails per boot and one
 * compressed firmware per install, and programs only 16-byte attempt marks, so it buys
 * nothing from quad mode and owes nothing to the part's QE bit or dummy-cycle settings.
 * A part over 16 MiB is driven in 4-byte address mode (the 4P/PT's 32 MiB); the soft
 * reset at init and at deinit puts it back where the firmware's driver expects it.
 */
#include <string.h>
#include STM32_HAL_H
#include "stub.h"
#include "port.h"
#include "port_stm32.h"

#if defined(STM32H7)

#define QSPI_PAGE_SIZE          (0x100)
// READ (0x03/0x13) is good to 50 MHz on the parts fitted, but the margin is the controller's:
// sampling half a clock after the part drives leaves 20 ns at 25 MHz against an output delay
// of up to 8 ns, and 12.5 ns at 40 MHz. The boards whose bootloader clocks put the stub at
// 25 MHz (Portenta, Nicla) read 140 boots clean on the fleet; the Giga at 34-40 MHz twice
// found no part at all (reset timed out, both slots rejected). Everyone gets the 25 MHz margin;
// the cost is about 0.3 s more per install.
#define QSPI_MAX_HZ             (25000000)
#define QSPI_CMD_TIMEOUT        (1000)
#define QSPI_READ_CHUNK         (0x10000)       // one transfer per timeout
#define QSPI_RESET_RETRIES      (3)             // reset attempts after the first, 10/20/40 ms apart

#define CMD_WRITE_ENABLE        (0x06)
#define CMD_READ_STATUS         (0x05)
#define CMD_READ_ID             (0x9F)
#define CMD_READ_3B             (0x03)
#define CMD_READ_4B             (0x13)
#define CMD_PAGE_PROG_3B        (0x02)
#define CMD_PAGE_PROG_4B        (0x12)
#define CMD_RESET_ENABLE        (0x66)
#define CMD_RESET_MEMORY        (0x99)
#define CMD_ADDR4_ENABLE        (0xB7)

#define SR_WIP                  (1U << 0)
#define SR_WEL                  (1U << 1)

#define ADDR4                   (STUB_QSPI_FLASH_SIZE > 0x1000000U)

static QSPI_HandleTypeDef qspi;
static bool inited = false;

// The kernel clock source the stub found selected (restored at deinit). The firmware runs the
// controller from PLL2; the stub runs it from the AHB clock, which is alive for as long as the
// stub is, whatever a reset left of the PLLs.
static uint32_t inherited_qspisel;

// The controller as the previous owner (the bootloader, or the firmware across a core reset)
// handed it over, read before this driver's first peripheral reset: whether its clock was on
// and, if so, CR / SR / CCR / AR / DLR / DCR. Logged every boot in three lines, so a boot that
// wedges can be set beside the boots that did not.
static bool inherited_clk;
static uint32_t inherited_cr, inherited_sr, inherited_ccr, inherited_ar, inherited_dlr;
static uint32_t inherited_dcr;

static void snapshot_inherited(void) {
    inherited_clk = (RCC->AHB3ENR & RCC_AHB3ENR_QSPIEN) != 0;
    if (!inherited_clk) {
        log_line("qspi inherited clk off");
        return;
    }
    inherited_cr = QUADSPI->CR;
    inherited_sr = QUADSPI->SR;
    inherited_ccr = QUADSPI->CCR;
    inherited_ar = QUADSPI->AR;
    inherited_dlr = QUADSPI->DLR;
    inherited_dcr = QUADSPI->DCR;
    log_kv("qspi inherited cr", inherited_cr);
    log_kv("qspi inherited sr", inherited_sr);
    log_kv("qspi inherited ccr", inherited_ccr);
}

// Everything a failed reset should say about the clock tree and the controller, once.
static void dump_state(void) {
    log_kv("qspi sel inherited", inherited_qspisel);
    log_kv("qspi inherited ar", inherited_ar);
    log_kv("qspi inherited dlr", inherited_dlr);
    log_kv("qspi inherited dcr", inherited_dcr);
    log_kv("flash optsr", FLASH->OPTSR_CUR);
    log_kv("rcc ahb3rstr", RCC->AHB3RSTR);
    log_kv("rcc cr", RCC->CR);
    log_kv("rcc cfgr", RCC->CFGR);
    log_kv("rcc d1cfgr", RCC->D1CFGR);
    log_kv("rcc d1ccipr", RCC->D1CCIPR);
    log_kv("rcc ahb3enr", RCC->AHB3ENR);
    log_kv("rcc gcr", RCC->GCR);
    log_kv("qspi cr", QUADSPI->CR);
    log_kv("qspi dcr", QUADSPI->DCR);
    log_kv("qspi ccr", QUADSPI->CCR);
    log_kv("dbgmcu cr", DBGMCU->CR);
}

static QSPI_CommandTypeDef command(uint8_t instruction) {
    QSPI_CommandTypeDef c = {
        .InstructionMode = QSPI_INSTRUCTION_1_LINE,
        .Instruction = instruction,
        .AddressMode = QSPI_ADDRESS_NONE,
        .AlternateByteMode = QSPI_ALTERNATE_BYTES_NONE,
        .DataMode = QSPI_DATA_NONE,
        .DummyCycles = 0,
        .DdrMode = QSPI_DDR_MODE_DISABLE,
        .DdrHoldHalfCycle = QSPI_DDR_HHC_ANALOG_DELAY,
        .SIOOMode = QSPI_SIOO_INST_EVERY_CMD,
    };
    return c;
}

static QSPI_CommandTypeDef command_at(uint8_t inst3, uint8_t inst4, uint32_t addr, uint32_t n) {
    QSPI_CommandTypeDef c = command(ADDR4 ? inst4 : inst3);
    c.AddressMode = QSPI_ADDRESS_1_LINE;
    c.AddressSize = ADDR4 ? QSPI_ADDRESS_32_BITS : QSPI_ADDRESS_24_BITS;
    c.Address = addr;
    c.DataMode = QSPI_DATA_1_LINE;
    c.NbData = n;
    return c;
}

static int send(QSPI_CommandTypeDef *c) {
    return (HAL_QSPI_Command(&qspi, c, QSPI_CMD_TIMEOUT) == HAL_OK) ? 0 : -1;
}

static int poll_status(uint32_t mask, uint32_t match, uint32_t timeout) {
    QSPI_AutoPollingTypeDef cfg = {
        .Mask = mask,
        .Match = match,
        .MatchMode = QSPI_MATCH_MODE_AND,
        .Interval = 0x10,
        .AutomaticStop = QSPI_AUTOMATIC_STOP_ENABLE,
        .StatusBytesSize = 1,
    };
    QSPI_CommandTypeDef c = command(CMD_READ_STATUS);
    c.DataMode = QSPI_DATA_1_LINE;
    return (HAL_QSPI_AutoPolling(&qspi, &c, &cfg, timeout) == HAL_OK) ? 0 : -1;
}

static int write_enable(void) {
    QSPI_CommandTypeDef c = command(CMD_WRITE_ENABLE);
    if (send(&c) != 0) {
        return -1;
    }
    return poll_status(SR_WEL, SR_WEL, QSPI_CMD_TIMEOUT);
}

// The controller from its reset state: an RCC peripheral reset, then the HAL's init at the
// slowest prescaler QSPI_MAX_HZ allows. Also the way back from a HAL call that timed out: the
// HAL leaves its handle in the error state and the controller BUSY (an auto-poll that never
// matched keeps polling until it is aborted), and every later call refuses. A peripheral reset
// needs no cooperation from either, which the HAL's own abort would (and it drags in MDMA).
static int controller_init(void) {
    __HAL_RCC_QSPI_CLK_ENABLE();
    __HAL_RCC_QSPI_FORCE_RESET();
    __HAL_RCC_QSPI_RELEASE_RESET();
    __HAL_RCC_QSPI_CONFIG(RCC_QSPICLKSOURCE_D1HCLK);

    uint32_t presc = (HAL_RCC_GetHCLKFreq() + QSPI_MAX_HZ - 1) / QSPI_MAX_HZ;
    if (presc < 1) {
        presc = 1;
    }
    if (presc > 256) {
        presc = 256;
    }
    memset(&qspi, 0, sizeof(qspi));
    qspi.Instance = QUADSPI;
    qspi.Init.ClockPrescaler = presc - 1;
    qspi.Init.FifoThreshold = 4;
    // No sample shift: the controller samples half a clock after the part drives, which at
    // <= 40 MHz leaves 12 ns or more over the part's output delay. The half-cycle shift the
    // firmware's 100 MHz driver needs would sample right on the edge where the part drives
    // the next bit -- the Nicla's AT25SL128A tore trailer CRCs and even its JEDEC id that way.
    qspi.Init.SampleShifting = QSPI_SAMPLE_SHIFTING_NONE;
    qspi.Init.FlashSize = __builtin_ctz(STUB_QSPI_FLASH_SIZE) - 1;
    qspi.Init.ChipSelectHighTime = QSPI_CS_HIGH_TIME_2_CYCLE;
    qspi.Init.ClockMode = QSPI_CLOCK_MODE_0;
    qspi.Init.FlashID = QSPI_FLASH_ID_1;
    qspi.Init.DualFlash = QSPI_DUALFLASH_DISABLE;
    if (HAL_QSPI_Init(&qspi) != HAL_OK) {
        log_line("qspi init failed");
        return -1;
    }
    return 0;
}

// One 1-line, address-less read of n bytes (RDSR, RDID). -1 when the controller did not
// complete it; a part that answers nothing is not an error here (an idle 1-line bus reads 0xFF).
static int read_bytes(uint8_t instruction, uint8_t *buf, uint32_t n) {
    QSPI_CommandTypeDef c = command(instruction);
    c.DataMode = QSPI_DATA_1_LINE;
    c.NbData = n;
    if (send(&c) != 0 || HAL_QSPI_Receive(&qspi, buf, QSPI_CMD_TIMEOUT) != HAL_OK) {
        return -1;
    }
    return 0;
}

// The part's JEDEC id as one word (0xFFFFFFFF when the controller could not read it): the
// first proof the bus samples cleanly (a torn id was the tell on the Nicla), and the one
// answer a part gives in any state short of deep power-down.
static uint32_t read_id(void) {
    uint8_t id[3] = {0};
    if (read_bytes(CMD_READ_ID, id, 3) != 0) {
        return 0xFFFFFFFFU;
    }
    return ((uint32_t) id[0] << 16) | ((uint32_t) id[1] << 8) | id[2];
}

// Power-on state: 3-byte addressing, write-disabled, no operation in progress.
//
// The part is whatever the previous owner left it: the firmware's driver (1-line commands,
// continuous read off) or the Arduino DFU bootloader's, which may have switched it to QPI,
// where it ignores 1-line instructions altogether. A reset is the one thing a QPI part still
// obeys, so the pair goes out on 1 line and then on 4, and only then is the part asked for
// its status.
//
// 1 line FIRST. With the 4-line pair first, the Portenta's controller stuck on that very
// first instruction after a debug-core reset of the running firmware -- BUSY set and the
// transfer never complete, every retry the same, no status or id to be had -- on one boot in
// ten (2 of 20 on the bench, the fleet's "qspi reset timeout" legs), with the inherited
// controller state identical to a clean boot's. The 1-line pair first: 0 of 60 such resets.
// Why the controller minds which instruction comes first is not known; the order is what is
// measured, and the 4-line pair still follows for a part left in QPI.
//
// Returns 0, or the step that failed: 1-4 the four reset instructions (the controller never
// finished sending one), 5 the status poll (the part never reported idle, or never answered).
static int chip_reset(void) {
    static const uint32_t modes[] = { QSPI_INSTRUCTION_1_LINE, QSPI_INSTRUCTION_4_LINES };
    for (int i = 0; i < (int) (sizeof(modes) / sizeof(modes[0])); i++) {
        QSPI_CommandTypeDef c = command(CMD_RESET_ENABLE);
        c.InstructionMode = modes[i];
        if (send(&c) != 0) {
            return 2 * i + 1;
        }
        c = command(CMD_RESET_MEMORY);
        c.InstructionMode = modes[i];
        if (send(&c) != 0) {
            return 2 * i + 2;
        }
        port_delay_ms(1);                       // tRST: 30 us on the parts fitted
    }
    return (poll_status(SR_WIP, 0, QSPI_CMD_TIMEOUT) == 0) ? 0 : 5;
}

int qspi_flash_init(void) {
    if (inited) {
        return 0;
    }
    inherited_qspisel = __HAL_RCC_GET_QSPI_SOURCE();
    snapshot_inherited();
    if (controller_init() != 0) {
        return -1;
    }
    log_kv("qspi presc", qspi.Init.ClockPrescaler + 1);
    int step = chip_reset();
    for (int attempt = 0; step != 0; attempt++) {
        // The reset sequence did not end with the part reporting idle. Say where it stopped
        // and what the controller holds, bring the controller back with a peripheral reset
        // (the failed HAL call leaves it refusing everything), let the part settle, and ask
        // it for its status and id before trying again -- the two answers that tell a silent
        // part (0xFF: nothing drives the line) from a busy one (WIP set: a program or erase
        // the reset did not interrupt) or a torn read. The fleet's Portenta loses about one
        // boot in six this way at the harness's first debug-core reset after a DFU
        // provisioning, never by hand, and boots from the install record without its slots
        // when it does.
        log_kv("qspi reset failed at step", step);
        log_kv("qspi hal err", qspi.ErrorCode);
        log_kv("qspi sr", QUADSPI->SR);
        if (attempt == 0) {
            dump_state();
        }
        if (attempt == QSPI_RESET_RETRIES) {
            log_line("qspi reset timeout");     // slot reads will fail; the record decides
            return -1;
        }
        port_delay_ms(10U << attempt);
        if (controller_init() != 0) {
            return -1;
        }
        uint8_t sr = 0;
        log_kv("qspi status", (read_bytes(CMD_READ_STATUS, &sr, 1) == 0) ? sr : 0x100);
        log_kv("qspi jedec", read_id());
        step = chip_reset();
        if (step == 0) {
            log_line("qspi reset retry ok");
        }
    }
    log_kv("qspi jedec", read_id());
    if (ADDR4) {
        if (write_enable() != 0) {
            return -1;
        }
        QSPI_CommandTypeDef c = command(CMD_ADDR4_ENABLE);
        if (send(&c) != 0 || poll_status(SR_WIP, 0, QSPI_CMD_TIMEOUT) != 0) {
            log_line("qspi addr4 failed");
            return -1;
        }
    }
    inited = true;
    return 0;
}

int qspi_flash_deinit(void) {
    if (!inited) {
        return 0;
    }
    (void) chip_reset();                        // 3-byte mode again for the firmware's driver
    HAL_QSPI_DeInit(&qspi);
    __HAL_RCC_QSPI_FORCE_RESET();
    __HAL_RCC_QSPI_RELEASE_RESET();
    __HAL_RCC_QSPI_CLK_DISABLE();
    __HAL_RCC_QSPI_CONFIG(inherited_qspisel);
    memset(&qspi, 0, sizeof(qspi));
    inited = false;
    return 0;
}

int qspi_flash_read(uint32_t addr, uint8_t *buf, uint32_t size) {
    if (!inited && qspi_flash_init() != 0) {
        return -1;
    }
    while (size) {
        uint32_t n = (size < QSPI_READ_CHUNK) ? size : QSPI_READ_CHUNK;
        QSPI_CommandTypeDef c = command_at(CMD_READ_3B, CMD_READ_4B, addr, n);
        if (send(&c) != 0 || HAL_QSPI_Receive(&qspi, buf, QSPI_CMD_TIMEOUT) != HAL_OK) {
            return -1;
        }
        addr += n;
        buf += n;
        size -= n;
    }
    return 0;
}

static int write_page(uint32_t addr, const uint8_t *buf, uint32_t n) {
    if (write_enable() != 0) {
        return -1;
    }
    QSPI_CommandTypeDef c = command_at(CMD_PAGE_PROG_3B, CMD_PAGE_PROG_4B, addr, n);
    if (send(&c) != 0 || HAL_QSPI_Transmit(&qspi, (uint8_t *) buf, QSPI_CMD_TIMEOUT) != HAL_OK) {
        return -1;
    }
    return poll_status(SR_WIP, 0, QSPI_CMD_TIMEOUT);
}

// Program over erased bytes, page by page (no erase: the stub only ever adds marks to a
// slot's status sector).
int qspi_flash_write(uint32_t addr, const uint8_t *buf, uint32_t size) {
    if (!inited && qspi_flash_init() != 0) {
        return -1;
    }
    while (size) {
        uint32_t room = QSPI_PAGE_SIZE - (addr % QSPI_PAGE_SIZE);
        uint32_t n = (size < room) ? size : room;
        if (write_page(addr, buf, n) != 0) {
            return -1;
        }
        addr += n;
        buf += n;
        size -= n;
    }
    return 0;
}

#endif // STM32H7

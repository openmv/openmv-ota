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

// The controller's kernel clock, from whatever tree the bootloader left running.
static uint32_t kernel_clock(void) {
    switch (__HAL_RCC_GET_QSPI_SOURCE()) {
        case RCC_QSPICLKSOURCE_D1HCLK:
            return HAL_RCC_GetHCLKFreq();
        case RCC_QSPICLKSOURCE_PLL: {
            PLL1_ClocksTypeDef c;
            HAL_RCCEx_GetPLL1ClockFreq(&c);
            return c.PLL1_Q_Frequency;
        }
        case RCC_QSPICLKSOURCE_PLL2: {
            PLL2_ClocksTypeDef c;
            HAL_RCCEx_GetPLL2ClockFreq(&c);
            return c.PLL2_R_Frequency;
        }
        default: {
            uint32_t sel = __HAL_RCC_GET_CLKP_SOURCE();
            if (sel == RCC_CLKPSOURCE_HSI) {
                return HSI_VALUE >> ((RCC->CR & RCC_CR_HSIDIV) >> RCC_CR_HSIDIV_Pos);
            }
            return (sel == RCC_CLKPSOURCE_CSI) ? CSI_VALUE : HSE_VALUE;
        }
    }
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

// Power-on state: 3-byte addressing, write-disabled, no operation in progress.
//
// The part is whatever the previous owner left it: the firmware's driver (1-line commands,
// continuous read off) or the Arduino DFU bootloader's, which may have switched it to QPI,
// where it ignores 1-line instructions altogether. A reset is the one thing a QPI part still
// obeys, so send the pair on 4 lines first (a no-op to a part in SPI mode, which sees nothing
// it recognises) and then on 1 line, and only then ask the part for its status.
static int chip_reset(void) {
    static const uint32_t modes[] = { QSPI_INSTRUCTION_4_LINES, QSPI_INSTRUCTION_1_LINE };
    for (size_t i = 0; i < sizeof(modes) / sizeof(modes[0]); i++) {
        QSPI_CommandTypeDef c = command(CMD_RESET_ENABLE);
        c.InstructionMode = modes[i];
        if (send(&c) != 0) {
            return -1;
        }
        c = command(CMD_RESET_MEMORY);
        c.InstructionMode = modes[i];
        if (send(&c) != 0) {
            return -1;
        }
        port_delay_ms(1);                       // tRST: 30 us on the parts fitted
    }
    return poll_status(SR_WIP, 0, QSPI_CMD_TIMEOUT);
}

int qspi_flash_init(void) {
    if (inited) {
        return 0;
    }
    __HAL_RCC_QSPI_CLK_ENABLE();
    __HAL_RCC_QSPI_FORCE_RESET();
    __HAL_RCC_QSPI_RELEASE_RESET();

    uint32_t presc = (kernel_clock() + QSPI_MAX_HZ - 1) / QSPI_MAX_HZ;
    if (presc < 1) {
        presc = 1;
    }
    if (presc > 256) {
        presc = 256;
    }
    log_kv("qspi presc", presc);
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
    if (chip_reset() != 0) {
        log_line("qspi reset timeout");         // no part answered on 1 line: slot reads will fail
        return -1;
    }
    {
        // The part's JEDEC id, read right after the reset: the first proof the bus samples
        // cleanly (a torn id was the tell on the Nicla).
        uint8_t id[4] = {0};
        QSPI_CommandTypeDef c = command(CMD_READ_ID);
        c.DataMode = QSPI_DATA_1_LINE;
        c.NbData = 3;
        if (send(&c) == 0) {
            HAL_QSPI_Receive(&qspi, id, QSPI_CMD_TIMEOUT);
        }
        log_kv("qspi jedec", ((uint32_t) id[0] << 16) | ((uint32_t) id[1] << 8) | id[2]);
    }
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

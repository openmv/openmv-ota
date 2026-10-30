# OpenMV boot stub -- STM32 port build rules. Included by stub/Makefile.
#
# One port, two flash models: the N6 runs from and installs into one external octal
# flash (xspi.c, the bootloader's driver), the H7 boards install into internal flash and
# read their slots off a QSPI part (qspi.c). HAL_SERIES from the board picks the set.

HAL_DIR   := $(OMV_DIR)/lib/stm32/$(HAL_SERIES)
CMSIS_DIR := $(OMV_DIR)/lib/cmsis
LDSCRIPT  := $(PORT_DIR)/stm32.ld.S

CFLAGS += -std=gnu11 \
          -Wall \
          -Werror \
          -Warray-bounds \
          -nostartfiles \
          -fdata-sections \
          -ffunction-sections \
          -fno-inline-small-functions \
          -mthumb \
          -mcpu=$(CPU) \
          -mtune=$(CPU) \
          -mfpu=$(FPU) \
          -mfloat-abi=hard

CFLAGS += -D$(MCU) \
          -DHSE_VALUE=$(HSE_VALUE) \
          -DUSE_FULL_LL_DRIVER \
          -DCMSIS_MCU_H='<$(MCU_LOWER).h>' \
          -DSTM32_HAL_H='<stm32$(HAL_SERIES)xx_hal.h>'

CFLAGS += -I$(OMV_DIR)/boards/$(BOARD) \
          -I$(CMSIS_DIR)/include \
          -I$(CMSIS_DIR)/include/st \
          -I$(HAL_DIR)/include \
          -I$(HAL_DIR)/include/Legacy

LDFLAGS += -mthumb \
           -mcpu=$(CPU) \
           -mfpu=$(FPU) \
           -mfloat-abi=hard \
           -mabi=aapcs-linux \
           -Wl,--print-memory-usage \
           -Wl,--gc-sections \
           -Wl,-T$(BUILD)/stub.lds \
           -Wl,-Map=$(BUILD)/stub.map

SRC_C += $(PORT_DIR)/port.c

SRC_S += $(PORT_DIR)/start.s

SRC_C += $(addprefix $(HAL_DIR)/src/, \
         stm32$(HAL_SERIES)xx_hal.c \
         stm32$(HAL_SERIES)xx_hal_cortex.c \
         stm32$(HAL_SERIES)xx_hal_gpio.c \
         stm32$(HAL_SERIES)xx_hal_rcc.c \
         stm32$(HAL_SERIES)xx_hal_rcc_ex.c \
         stm32$(HAL_SERIES)xx_hal_iwdg.c \
         stm32$(HAL_SERIES)xx_ll_rcc.c \
         )

ifeq ($(HAL_SERIES),n6)
CFLAGS += -DHAL_IWDG_MODULE_ENABLED      # the N6 HAL config leaves the IWDG module out
SRC_C += $(PORT_DIR)/xspi.c \
         $(HAL_DIR)/src/stm32n6xx_hal_xspi.c
endif

ifeq ($(HAL_SERIES),h7)
SRC_C += $(PORT_DIR)/qspi.c \
         $(addprefix $(HAL_DIR)/src/, \
         stm32h7xx_hal_qspi.c \
         stm32h7xx_hal_flash.c \
         stm32h7xx_hal_flash_ex.c \
         )
endif

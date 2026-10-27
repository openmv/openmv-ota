# OpenMV boot stub -- STM32 port build rules. Included by stub/Makefile.

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
          -DHAL_IWDG_MODULE_ENABLED \
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

SRC_C += $(PORT_DIR)/port.c \
         $(PORT_DIR)/xspi.c

SRC_S += $(PORT_DIR)/start.s

SRC_C += $(addprefix $(HAL_DIR)/src/, \
         stm32$(HAL_SERIES)xx_hal.c \
         stm32$(HAL_SERIES)xx_hal_cortex.c \
         stm32$(HAL_SERIES)xx_hal_gpio.c \
         stm32$(HAL_SERIES)xx_hal_rcc.c \
         stm32$(HAL_SERIES)xx_hal_rcc_ex.c \
         stm32$(HAL_SERIES)xx_hal_xspi.c \
         stm32$(HAL_SERIES)xx_hal_iwdg.c \
         stm32$(HAL_SERIES)xx_ll_rcc.c \
         )

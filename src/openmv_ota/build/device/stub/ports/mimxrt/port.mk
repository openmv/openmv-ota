# OpenMV boot stub -- i.MX RT port build rules. Included by stub/Makefile.
#
# One flash model: the firmware and the slots share the QSPI NOR part on FlexSPI, read through
# the memory map and programmed with the chip ROM's FlexSPI NOR driver (fsl_romapi, a table of
# function pointers in the ROM) from the FCB the ROM itself booted with. The vendor headers
# come from the firmware checkout's lib/mimxrt and lib/cmsis.
HAL_DIR   := $(OMV_DIR)/lib/mimxrt/$(MCU_SERIES)
CMSIS_DIR := $(OMV_DIR)/lib/cmsis
LDSCRIPT  := common/stub.ld.S

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

CFLAGS += -DCPU_$(MCU) \
          -D__FPU_PRESENT=1

CFLAGS += -I$(CMSIS_DIR)/include \
          -I$(CMSIS_DIR)/include/mimxrt \
          -I$(HAL_DIR) \
          -I$(HAL_DIR)/drivers

LDFLAGS += -mthumb \
           -mcpu=$(CPU) \
           -mfpu=$(FPU) \
           -mfloat-abi=hard \
           -mabi=aapcs-linux \
           -Wl,--print-memory-usage \
           -Wl,--gc-sections \
           -Wl,--no-warn-rwx-segment \
           -Wl,-T$(BUILD)/stub.lds \
           -Wl,-Map=$(BUILD)/stub.map

SRC_C += $(PORT_DIR)/port.c \
         $(HAL_DIR)/drivers/fsl_romapi.c \
         $(CMSIS_DIR)/src/mimxrt/system_$(MCU_SERIES).c
SRC_S += common/start.s

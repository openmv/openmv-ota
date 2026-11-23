# OpenMV boot stub -- Alif Ensemble port build rules. Included by stub/Makefile.
#
# One flash model: the firmware (the HP and HE core images) lives in the on-chip MRAM,
# programmed in 16-byte lines through the memory map; the slots are on the OSPI part,
# read and marked through the controller in 1-line SPI mode (alif_ospi.c, after the
# bootloader's driver). The vendor headers come from the firmware checkout's lib/alif-dfp
# (the Alif DFP, v2 layout: per-SoC headers under Device/soc, per-core ones under rtss_*).

HAL_DIR   := $(OMV_DIR)/lib/alif-dfp
RTSS_CORE := RTSS_$(word 2,$(subst _, ,$(MCU_CORE)))
RTSS_DIR  := $(shell echo $(RTSS_CORE) | tr A-Z a-z)
SOC_DIR   := $(HAL_DIR)/Device/soc/$(MCU_VARIANT)
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
          -mfloat-abi=hard \
          -march=armv8.1-m.main+fp+mve.fp

CFLAGS += -D$(MCU_CORE) \
          -DCORE_$(MCU_CORE)=1 \
          -D$(RTSS_CORE) \
          -DALIF_CMSIS_H='<sys_utils.h>'

CFLAGS += -I$(CMSIS_DIR)/include \
          -I$(HAL_DIR)/drivers/include \
          -I$(HAL_DIR)/ospi_xip/source/ospi \
          -I$(HAL_DIR)/Device/core/common/config \
          -I$(HAL_DIR)/Device/core/common/include \
          -I$(HAL_DIR)/Device/core/$(RTSS_DIR)/config \
          -I$(HAL_DIR)/Device/system/include \
          -I$(SOC_DIR)/config \
          -I$(SOC_DIR)/include \
          -I$(SOC_DIR)/include/$(RTSS_DIR) \
          -I$(SOC_DIR)/rte

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
         $(PORT_DIR)/alif_ospi.c \
         $(PORT_DIR)/ecb_slot.c \
         lib/aes/flash_key.c \
         lib/aes/aes128.c

CFLAGS += -Ilib/aes

SRC_S += common/start.s

SRC_C += $(addprefix $(HAL_DIR)/drivers/source/, \
         pinconf.c \
         uart.c \
         )

# The HAL's drivers/source is on the VPATH for pinconf.c and uart.c, and it has an ospi.c
# of its own: the stub's driver takes a name that cannot collide with it.

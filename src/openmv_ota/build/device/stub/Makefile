# OpenMV boot stub.
#
#   make OMV_DIR=<openmv checkout> BOARD=OPENMV_N6 KEYS_DIR=<dir with stub_keys.h>
#
# Builds $(BUILD)/stub.bin: the loader the OTA tool flashes at the firmware address.
# The openmv checkout supplies the vendor HAL and CMSIS; everything else is here.
# `openmv-ota build stub` drives this with the project's keys and toolchain.

BOARD    ?= OPENMV_N6
OMV_DIR  ?= $(error OMV_DIR must point at an openmv checkout)
KEYS_DIR ?= $(error KEYS_DIR must hold the project's stub_keys.h)
BUILD    ?= $(CURDIR)/build/$(BOARD)
DEBUG    ?= 0

include boards/$(BOARD)/board.mk

PORT_DIR := ports/$(PORT)
CROSS_COMPILE ?= arm-none-eabi-
CC      := $(CROSS_COMPILE)gcc
AS      := $(CROSS_COMPILE)gcc
CPP     := $(CROSS_COMPILE)gcc -E
OBJCOPY := $(CROSS_COMPILE)objcopy
SIZE    := $(CROSS_COMPILE)size
MCU_LOWER := $(shell echo $(MCU) | tr '[:upper:]' '[:lower:]')

ifeq ($(DEBUG), 1)
CFLAGS := -Og -ggdb3 -MMD
else
CFLAGS := -O2 -DNDEBUG -MMD
endif

# A bench build: STUB_DEBUG_UART=<machine.UART id> opens that UART before the slots are read,
# so a stub that rejects both slots (and so has no trailer to take a UART from) still talks.
ifdef STUB_DEBUG_UART
CFLAGS += -DSTUB_DEBUG_UART=$(STUB_DEBUG_UART)
endif

CFLAGS += $(EXTRA_CFLAGS) -Icommon \
          -I$(PORT_DIR) \
          -Iboards/$(BOARD) \
          -I$(KEYS_DIR) \
          -Ilib/lzma \
          -Ilib/p256-m \
          -Ilib/sha256

SRC_C := common/main.c \
         common/verify.c \
         common/install.c \
         common/log.c \
         lib/lzma/LzmaDec.c \
         lib/p256-m/p256-m.c \
         lib/sha256/sha256.c

include $(PORT_DIR)/port.mk

CPP_FLAGS := -P -E -DLINKER_SCRIPT -Iboards/$(BOARD)

OBJS := $(addprefix $(BUILD)/, $(notdir $(SRC_C:.c=.o)) $(notdir $(SRC_S:.s=.o)))
VPATH := $(sort $(dir $(SRC_C) $(SRC_S)))

all: $(BUILD)/stub.bin

$(BUILD):
	mkdir -p $@

$(BUILD)/%.o: %.c | $(BUILD)
	@echo "CC $(notdir $<)"
	@$(CC) $(CFLAGS) -c -o $@ $<

$(BUILD)/%.o: %.s | $(BUILD)
	@echo "AS $(notdir $<)"
	@$(AS) $(CFLAGS) -c -o $@ $<

$(BUILD)/stub.lds: $(LDSCRIPT) boards/$(BOARD)/stub_config.h | $(BUILD)
	$(CPP) $(CPP_FLAGS) $< > $@

$(BUILD)/stub.elf: $(OBJS) $(BUILD)/stub.lds
	$(CC) $(LDFLAGS) $(OBJS) -o $@
	$(SIZE) $@

$(BUILD)/stub.bin: $(BUILD)/stub.elf
	$(OBJCOPY) -Obinary $< $@

clean:
	rm -rf $(BUILD)

.PHONY: all clean

-include $(OBJS:%.o=%.d)

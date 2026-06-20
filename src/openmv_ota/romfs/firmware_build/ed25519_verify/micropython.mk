# MicroPython user-C-module makefile fragment for ed25519_verify.
#
# Wired into the openmv firmware build by Tool 1 so the module is compiled into
# firmware.bin. TODO: fill in once ed25519_verify.c is implemented.

ED25519_VERIFY_MOD_DIR := $(USERMOD_DIR)

SRC_USERMOD += $(ED25519_VERIFY_MOD_DIR)/ed25519_verify.c

CFLAGS_USERMOD += -I$(ED25519_VERIFY_MOD_DIR)

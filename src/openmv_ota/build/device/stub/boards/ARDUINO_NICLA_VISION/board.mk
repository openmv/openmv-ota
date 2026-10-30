# OpenMV boot stub -- ARDUINO_NICLA_VISION (STM32H747, Cortex-M7 core).
PORT       := stm32
MCU        := STM32H747xx
CPU        := cortex-m7
FPU        := fpv5-sp-d16
HAL_SERIES := h7
HSE_VALUE  := 25000000
EXTRA_CFLAGS += -DCORE_CM7

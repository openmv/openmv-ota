/*
 * OpenMV boot stub -- STM32 linker script (preprocessed with the board's stub_config.h).
 *
 * FLASH holds the image as the bootloader sees it: an 8-byte head (stack, entry) and
 * the copier run in place; everything else is loaded there and copied to CODE/RAM by
 * start.s. BUF is the big NOLOAD scratch (decoder dictionary, flash block buffers).
 */
ENTRY(Reset_Handler)

#include "stub_config.h"

MEMORY
{
  FLASH (rx)  : ORIGIN = STUB_FLASH_ORIGIN, LENGTH = STUB_FLASH_LENGTH
  CODE (rwx)  : ORIGIN = STUB_CODE_ORIGIN,  LENGTH = STUB_CODE_LENGTH
  RAM (rwx)   : ORIGIN = STUB_RAM_ORIGIN,   LENGTH = STUB_RAM_LENGTH
  BUF (rw)    : ORIGIN = STUB_BUF_ORIGIN,   LENGTH = STUB_BUF_LENGTH
}

_stack_size = STUB_STACK_SIZE;

SECTIONS
{
  .flash_head :
  {
    . = ALIGN(4);
    KEEP(*(.flash_head))
    . = ALIGN(4);
  } >FLASH

  .text :
  {
    . = ALIGN(16);
    _scode = .;
    KEEP(*(.isr_vector))
    *(.text)
    *(.text*)
    *(.rodata)
    *(.rodata*)
    . = ALIGN(4);
    _ecode = .;
  } >CODE AT> FLASH

  _sicode = LOADADDR(.text);

  .ARM.exidx :
  {
    *(.ARM.exidx*)
  } >CODE AT> FLASH

  .data :
  {
    . = ALIGN(4);
    _sdata = .;
    *(.data)
    *(.data*)
    . = ALIGN(4);
    _edata = .;
  } >RAM AT> FLASH

  _sidata = LOADADDR(.data);

  .bss :
  {
    . = ALIGN(4);
    _sbss = .;
    *(.bss)
    *(.bss*)
    *(COMMON)
    . = ALIGN(4);
    _ebss = .;
  } >RAM

  .stack :
  {
    . = ALIGN(8);
    _sstack = .;
    . = . + _stack_size;
    . = ALIGN(8);
    _estack = .;
  } >RAM

  .buffers (NOLOAD) :
  {
    . = ALIGN(32);
    *(.buffers)
    *(.buffers*)
    . = ALIGN(32);
  } >BUF

  .ARM.attributes 0 : { *(.ARM.attributes) }
}

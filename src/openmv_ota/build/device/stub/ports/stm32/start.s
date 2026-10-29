/*
 * OpenMV boot stub -- STM32 entry.
 *
 * The bootloader jumps to the word at the flash address + 4 and never sets the stack
 * pointer, so the head does. It runs in place (XIP), copies the rest of the image
 * into SRAM, zeroes bss, points VTOR at the SRAM vector table and enters main --
 * after which the flash controller can be reset under the stub's feet.
 */
.syntax unified
.thumb

/* ---- Flash-resident head: what the bootloader sees at the firmware address ---- */
.section .flash_head, "ax"
.global _flash_head
_flash_head:
  .word _estack
  .word Reset_Handler

  .thumb_func
  .global Reset_Handler
  .type Reset_Handler, %function
Reset_Handler:
  cpsid i
  ldr   r0, =_estack
  mov   sp, r0

  /* Whatever the bootloader left on (caches, MPU) that would make the copy below
     incoherent -- a port's choice, in flash beside this head (see stub_head_prep) */
  bl    stub_head_prep

  /* Code + rodata + vector table: flash -> SRAM */
  ldr   r0, =_scode
  ldr   r1, =_ecode
  ldr   r2, =_sicode
  bl    copy_words

  /* Initialised data: flash -> RAM */
  ldr   r0, =_sdata
  ldr   r1, =_edata
  ldr   r2, =_sidata
  bl    copy_words

  /* bss */
  ldr   r0, =_sbss
  ldr   r1, =_ebss
  movs  r2, #0
1:
  cmp   r0, r1
  bge   2f
  str   r2, [r0], #4
  b     1b
2:
  /* VTOR -> the SRAM copy of the vector table */
  ldr   r0, =_svector
  ldr   r1, =0xE000ED08
  str   r0, [r1]
  dsb
  isb
  cpsie i

  ldr   r0, =main
  blx   r0
  b     .

  /* r0 = dst, r1 = dst end, r2 = src */
  .thumb_func
copy_words:
  cmp   r0, r1
  bge   3f
  ldr   r3, [r2], #4
  str   r3, [r0], #4
  b     copy_words
3:
  bx    lr

/* ---- The vector table the stub runs with (in SRAM) ---- */
.section .isr_vector, "a"
.global _svector
_svector:
  .word _estack
  .word Reset_Handler
  .word NMI_Handler
  .word HardFault_Handler
  .word Default_Handler     /* MemManage */
  .word BusFault_Handler
  .word Default_Handler     /* UsageFault */
  .word Default_Handler     /* SecureFault */
  .word 0
  .word 0
  .word 0
  .word Default_Handler     /* SVCall */
  .word Default_Handler     /* DebugMonitor */
  .word 0
  .word Default_Handler     /* PendSV */
  .word SysTick_Handler
  .rept 200
  .word Default_Handler
  .endr

.section .text.Default_Handler, "ax"
  .thumb_func
  .global Default_Handler
Default_Handler:
  b     .

/* A port may handle these (the H7 reads a torn flash word as an ECC fault); by default
   they park like everything else. */
  .weak NMI_Handler
  .thumb_set NMI_Handler, Default_Handler
  .weak HardFault_Handler
  .thumb_set HardFault_Handler, Default_Handler
  .weak BusFault_Handler
  .thumb_set BusFault_Handler, Default_Handler

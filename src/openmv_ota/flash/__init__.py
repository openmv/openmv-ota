"""Flashing tools: push built artifacts onto a board over its programming interface.

Each board's ``boards.json`` ``flash`` block names a **backend** (the host tool that talks
to the board) and where each artifact lands. Phase 1 is the ``dfu`` backend (``dfu-util``),
which covers the OpenMV STM32 boards; the Alif (AE3) and i.MX (RT1060) backends slot in
behind the same interface later.

The argv each backend builds is a pure function (``flash.dfu``), so the exact device id /
alt-setting / address is unit-testable; the only side effect is ``flash.runner.run``.
"""

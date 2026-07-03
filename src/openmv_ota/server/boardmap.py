"""Translate a firmware board name into the board CODE the swd-ids registry stores.

The device reports ``board`` from ``/rom/system.json`` -- the firmware board name (``OPENMV_N6``,
``OPENMV4``, ...). swd-ids matches ``(board, id)`` **exactly** against short codes in its ``ids``
table (``N6``, ``H7``, ...), so the update server must translate before calling
``/api/v1/registration/verify`` -- otherwise every real device reads as unregistered.

swd-ids is the source of truth for these codes; keep this table in sync with it. Boards not in the
table pass through unchanged (so swd-ids simply won't match -> the device reads unregistered,
fail-closed) -- add them here, or override at runtime via ``board_code_overrides``, once their
swd-ids code is confirmed. Deliberately unmapped: the retired Nano boards (ARDUINO_NANO_33_BLE_SENSE,
ARDUINO_NANO_RP2040_CONNECT -- swd-ids tracks these as *unregistered* board types, never in `ids`)
and the MPS emulator targets (not real devices).
"""

from __future__ import annotations

DEFAULT_BOARD_CODES = {
    "OPENMV2": "M4",
    "OPENMV3": "M7",
    "OPENMV4": "H7",
    "OPENMV4P": "H7",
    "OPENMVPT": "H7",
    "OPENMV_RT1060": "IMXRT1060",
    "OPENMV_AE3": "AE3",
    "OPENMV_N6": "N6",
    "ARDUINO_PORTENTA_H7": "H7",
    "ARDUINO_GIGA": "H7",
    "ARDUINO_NICLA_VISION": "NICLAV",
}


def swd_ids_board_code(board, overrides=None):
    """The swd-ids code for a firmware ``board`` name. Overrides win over the built-in table;
    an unknown board passes through unchanged."""
    if overrides and board in overrides:
        return overrides[board]
    return DEFAULT_BOARD_CODES.get(board, board)

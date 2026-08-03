"""The romfs erase in `_ensure_cdc` is DESTRUCTIVE, and it is gated for a reason.

Erasing frees DFU on a board whose CDC is gone, which is the only lever left when the app itself
is what breaks the port. But it is only ever right BEFORE a flash: run post-flash it wipes the
golden image that was just written. That is not hypothetical -- a bench run flashed golden through
the bootloader's DFU window, found the CDC not yet back, and erased it again, so the scenario
failed with `golden did not mount a valid romfs` on a board that had just been provisioned.

These tests pin the gate and the call sites, because the failure is silent (a wiped board looks
exactly like a board that never flashed) and the blast radius is every scenario on every board.
"""

import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_CIHIL = os.path.abspath(os.path.join(_HERE, "..", "..", "ci", "hil"))
sys.path.insert(0, _CIHIL)
os.environ.setdefault("WIFI_SSID", "")
os.environ.setdefault("WIFI_PASSWORD", "")

import ota_cycle  # noqa: E402  (ci/hil, added to sys.path above)

_SRC = open(os.path.join(_CIHIL, "ota_cycle.py")).read()


def test_erase_is_opt_in():
    """Default OFF: a caller that hasn't thought about it must not destroy the board's image."""
    import inspect
    sig = inspect.signature(ota_cycle._ensure_cdc)
    assert sig.parameters["allow_erase"].default is False


def test_post_flash_ensure_cdc_never_erases():
    """_flash_dfu_cli's trailing recovery must NOT pass allow_erase -- it runs AFTER the write."""
    body = _SRC.split("def _flash_dfu_cli(")[1].split("\ndef ")[0]
    calls = re.findall(r"_ensure_cdc\([^)]*\)", body)
    assert calls, "expected _flash_dfu_cli to still recover the CDC"
    # the LAST call in the function is the post-flash one; it must not enable the erase
    assert "allow_erase" not in calls[-1], (
        "the post-flash _ensure_cdc must not erase -- it would wipe the golden just written: %r"
        % calls[-1])


def test_erase_path_is_reached_only_with_the_flag():
    """The destructive call must sit behind the flag, not before it."""
    body = _SRC.split("def _ensure_cdc(")[1].split("\ndef ")[0]
    guard = body.index("if not allow_erase:")
    erase = body.index("recover_erase_romfs(")
    assert guard < erase, "recover_erase_romfs must be gated by `if not allow_erase: return`"

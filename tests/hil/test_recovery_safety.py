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


def test_firmware_recovery_is_two_stage_and_zero_first():
    """A cycling board offers a window too short for a full image (measured: a direct write died at
    32%, then 36% of 2 MB). Writing a sector of zeros first invalidates the firmware, so the
    bootloader stops handing over and parks in DFU -- then the real write has all the time it needs.
    Order is the whole trick; a single-stage write here is the bug this replaced."""
    body = _SRC.split("def recover_firmware(")[1].split("\ndef ")[0]
    assert "stage 1/2" in body and "stage 2/2" in body
    assert body.index("stage 1/2") < body.index("stage 2/2")
    assert 'b"\\x00" * 4096' in body, "stage 1 must write a small sector of zeros"
    # stage 1 must go through the reset-catch (dfu-util -w started BEFORE the pulse)
    assert body.index("dfu_reset_catch") < body.index("stage 2/2")


def test_firmware_recovery_refuses_a_non_dfu_board():
    """The imx boards flash through their SBL, not DFU -- there is no firmware alt to write."""
    import ota_cycle as oc
    assert oc.recover_firmware("OPENMV_RT1060") is False


def test_partial_download_is_distinguished_from_never_started():
    """Only a download that died PARTWAY has corrupted the firmware. One that never began left the
    old image intact, and running a two-stage recovery there would destroy a working board."""
    import ota_cycle as oc
    partial = ("Download\t[========    ]  32%  647168 bytes"
               "dfu-util: Error during download get_status (LIBUSB_ERROR_IO)")
    assert oc._partial_download(partial) is True
    assert oc._partial_download("dfu-util: No DFU capable USB device available") is False
    assert oc._partial_download("") is False
    assert oc._partial_download(None) is False


def test_failed_dfu_flash_recovers_then_retries():
    """A partial write leaves firmware that guarantees the NEXT attempt fails the same way (32%,
    36%, 32% across three runs on the N6). The flash path must break that cycle, not repeat it."""
    body = _SRC.split("def _flash_dfu_cli(")[1].split("\ndef ")[0]
    assert "_partial_download(out)" in body
    assert "recover_firmware(board)" in body
    assert body.index("_partial_download(out)") < body.index("recover_firmware(board)")


def test_firmware_stage2_checks_the_park_instead_of_assuming_it(monkeypatch):
    """Stage 2 used a plain `dfu-util -w` on the assumption that stage 1 had parked the board. When
    it had not, that -w waited for a device that was never coming and burned the whole timeout
    (rc=124 after 400s on the N6). Check, and make a window when there isn't one."""
    body = _SRC.split("def recover_firmware(")[1].split("\ndef ")[0]
    stage2 = body.split("stage 2/2")[0]
    assert "_dfu_present()" in stage2, "stage 2 must verify the park before trusting a plain -w"
    assert "dfu_reset_catch" in body, "and must be able to MAKE a window when it is not parked"

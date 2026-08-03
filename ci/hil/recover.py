#!/usr/bin/env python3
"""Recover a bench board whose USB-CDC is gone -- the "dfu -w + nRST pulse" trick.

WHY THIS EXISTS (read before reaching for a power cycle):

Every normal flash path enters the bootloader with ``machine.bootloader()`` over the USB-CDC. That
is useless in the one case you most need it: when the CDC itself is broken. A board gets there by
running an app that wedges or owns the port -- on the H7 Plus, an app polling an unreachable OTA
server destabilised USB entirely, and then NO reflash could reach it. Reset alone does not help:
it reboots straight back into the same app.

The way out is that **the OpenMV bootloader presents a DFU window on EVERY reset**. So:

    1. start a dfu-util-backed command with ``-w`` (it blocks, waiting for a DFU device);
    2. a moment later, pulse the board's PHYSICAL nRST line via the J-Link;
    3. dfu-util catches the window and does its work.

``--in-bootloader`` tells the CLI not to try the (broken) CDC route first. Erasing the romfs is the
useful payload: with no bootable slot the app never runs, the CDC comes back, and a normal
``flash factory`` can reprovision. It needs no built artifacts, so it works even on a fresh node.

The harness calls this automatically (``_ensure_cdc`` -> ``recover_erase_romfs``); this script is
the same thing by hand, for when you are debugging a board directly.

    ./recover.py --board OPENMV4P            # erase romfs -> frees the CDC
    ./recover.py --board OPENMV4P --probe    # just report whether the CDC answers
    ./recover.py --board OPENMV4P --reset    # only pulse nRST (no DFU)
    ./recover.py --board OPENMV_N6 --firmware   # CORRUPT FIRMWARE: two-stage reflash

CORRUPT FIRMWARE IS A DIFFERENT ILLNESS, and it does not look like one. The board is not dead, it
is CYCLING: bootloader -> crash -> bootloader, re-enumerating every couple of seconds. Every USB
operation then dies partway and the errors point everywhere but the cause -- dfu-util
LIBUSB_ERROR_IO a third of the way in, an i.MX SBL that "could not be claimed", ttyUSB renumbering,
a CDC that comes and goes between probes. It reads as flaky hardware. It is deterministic.

``--firmware`` fixes it in two stages: write a sector of ZEROS to the firmware alt (small enough to
fit the short window a cycling board offers) so nothing valid boots and the bootloader parks in DFU;
then write the real firmware with the board sitting still. Measured on the N6: a direct full write
died at 32%, then 36%; two-stage completed and the board came back with a working CDC.

Env (same as ota_cycle): BOARD_ACM, HIL_PROJECT, OPENMV_SDK, JLINK, DFU_UTIL.
"""

from __future__ import annotations

import argparse
import sys

import ota_cycle as oc


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--board", required=True, choices=sorted(oc.BOARDS),
                    help="which bench board to recover")
    ap.add_argument("--probe", action="store_true",
                    help="only report whether the CDC answers; change nothing")
    ap.add_argument("--reset", action="store_true",
                    help="only pulse nRST via the J-Link (no DFU, no erase)")
    ap.add_argument("--firmware", action="store_true",
                    help="reflash MAIN FIRMWARE in two stages -- for a board cycling "
                         "bootloader->crash->bootloader (see the module docstring)")
    ap.add_argument("--firmware-image", default=None,
                    help="firmware .bin to write (default: the project's build output)")
    args = ap.parse_args(argv)

    rc, _ = oc.sh([oc.ota("mpremote"), "connect", oc.CFG["acm"], "eval", "True"],
                  timeout=15, check=False, quiet=True)
    oc.log("probe: %s CDC at %s -> %s" % (args.board, oc.CFG["acm"],
                                          "responsive" if rc == 0 else "MISSING/unresponsive"))
    if args.probe:
        return 0 if rc == 0 else 1

    if args.reset:
        return 0 if oc.jlink_reset_pulse(args.board) else 1

    if args.firmware:
        # Deliberately NOT gated on the probe above: a cycling board often answers the CDC
        # intermittently, so "responsive" here means nothing. If you asked for a firmware
        # reflash, you have already decided the firmware is the problem.
        return 0 if oc.recover_firmware(args.board, args.firmware_image) else 1

    if rc == 0:
        oc.log("recover: CDC already responsive -- nothing to do "
               "(pass --reset to pulse nRST anyway)")
        return 0

    # The escalation the harness uses: reset first (cheap, fixes a merely-halted core), then erase
    # the romfs over DFU (the only thing that helps when the APP is what breaks the CDC).
    oc.jlink_reset_pulse(args.board)
    rc, _ = oc.sh([oc.ota("mpremote"), "connect", oc.CFG["acm"], "eval", "True"],
                  timeout=15, check=False, quiet=True)
    if rc == 0:
        oc.log("recover: CDC back after nRST alone")
        return 0
    if not oc.recover_erase_romfs(args.board):
        oc.log("recover: romfs erase FAILED -- the board may need a physical power cycle")
        return 1
    rc, _ = oc.sh([oc.ota("mpremote"), "connect", oc.CFG["acm"], "eval", "True"],
                  timeout=15, check=False, quiet=True)
    oc.log("recover: CDC %s after romfs erase" % ("BACK" if rc == 0 else "STILL GONE"))
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

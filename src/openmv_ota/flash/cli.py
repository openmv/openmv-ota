"""CLI handlers for the ``openmv-ota flash`` command group.

    firmware   flash the firmware image (both cores on the AE3 -- they're inseparable)
    romfs      flash the app romfs image
    factory    flash the manufacturing program: firmware + the dual-slot factory image
    bootloader flash the bootloader (board must be in system ROM DFU, entered by hand)
    erase      erase the onboard filesystem (the user disk)

One board at a time (the connected device); ``--dry-run`` prints the dfu-util commands
without running them.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import flash as flash_mod
from .errors import FlashError


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("project", nargs="?", default=".", help="project directory (default: .)")
    p.add_argument("-b", "--board", required=True, metavar="NAME",
                   help="the board to flash (the connected device)")
    p.add_argument("-o", "--output", help="artifact dir (default: <project>/build)")
    p.add_argument("--dfu-util", help="path to dfu-util (default: SDK's, else PATH)")
    p.add_argument("--sdk-home", help="SDK home to find the flash tools under "
                   "(dfu-util in bin/, sdphost/blhost in python/bin/)")
    p.add_argument("--no-reset", dest="reset", action="store_false",
                   help="don't reset (reboot) the board after flashing (dfu boards)")
    p.add_argument("--in-bootloader", dest="enter_bootloader", action="store_false",
                   help="the board is already in its bootloader; skip detecting + resetting "
                        "the running camera")
    p.add_argument("--serial", metavar="SN",
                   help="USB serial number of the camera to flash (when several are attached)")
    p.add_argument("--mpremote", help="path to mpremote (default: python -m mpremote)")
    p.add_argument("--dry-run", action="store_true",
                   help="print the dfu-util commands without running them")


def register(flash_parser: argparse.ArgumentParser):
    sub = flash_parser.add_subparsers(dest="_subcommand")

    p_fw = sub.add_parser("firmware", help="flash the firmware image")
    _add_common(p_fw)
    p_fw.set_defaults(func=cmd_firmware, _command="flash firmware")

    p_ro = sub.add_parser("romfs", help="flash the app romfs image")
    _add_common(p_ro)
    p_ro.set_defaults(func=cmd_romfs, _command="flash romfs")

    p_fa = sub.add_parser("factory", help="flash firmware + the dual-slot factory image")
    _add_common(p_fa)
    p_fa.set_defaults(func=cmd_factory, _command="flash factory")

    p_bl = sub.add_parser("bootloader", help="flash the bootloader (board in system ROM DFU)")
    _add_common(p_bl)
    p_bl.set_defaults(func=cmd_bootloader, _command="flash bootloader")

    p_er = sub.add_parser("erase", help="erase the onboard filesystem (the user disk)")
    _add_common(p_er)
    p_er.set_defaults(func=cmd_erase, _command="flash erase")
    return sub


def _sdk_home(args: argparse.Namespace) -> Path | None:
    return Path(args.sdk_home) if args.sdk_home else None


def _report(args: argparse.Namespace, steps) -> int:
    for s in steps:
        if args.dry_run:
            print("would run: %s" % " ".join(s.argv))
        elif getattr(s, "alt", None) is not None:          # a dfu step
            print("Flashed %s -> alt %d (%s)" % (s.file.name, s.alt, args.board))
        else:                                              # an imx step
            print("%s (%s)" % (s.label, args.board))
    return 0


def cmd_firmware(args: argparse.Namespace) -> int:
    try:
        steps = flash_mod.flash_firmware(
            args.project, board=args.board, output=args.output, dfu_util=args.dfu_util,
            sdk_home=_sdk_home(args), reset=args.reset, enter_bootloader=args.enter_bootloader,
            serial=args.serial, mpremote=args.mpremote,
            dry_run=args.dry_run)
    except FlashError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    return _report(args, steps)


def cmd_romfs(args: argparse.Namespace) -> int:
    try:
        steps = flash_mod.flash_romfs(
            args.project, board=args.board, output=args.output, dfu_util=args.dfu_util,
            sdk_home=_sdk_home(args), reset=args.reset, enter_bootloader=args.enter_bootloader,
            serial=args.serial, mpremote=args.mpremote, dry_run=args.dry_run)
    except FlashError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    return _report(args, steps)


def cmd_factory(args: argparse.Namespace) -> int:
    try:
        steps = flash_mod.flash_factory(
            args.project, board=args.board, output=args.output, dfu_util=args.dfu_util,
            sdk_home=_sdk_home(args), reset=args.reset, enter_bootloader=args.enter_bootloader,
            serial=args.serial, mpremote=args.mpremote,
            dry_run=args.dry_run)
    except FlashError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    return _report(args, steps)


def cmd_erase(args: argparse.Namespace) -> int:
    try:
        steps = flash_mod.flash_erase(
            args.project, board=args.board, dfu_util=args.dfu_util, sdk_home=_sdk_home(args),
            reset=args.reset, enter_bootloader=args.enter_bootloader, serial=args.serial,
            mpremote=args.mpremote, dry_run=args.dry_run)
    except FlashError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    return _report(args, steps)


def cmd_bootloader(args: argparse.Namespace) -> int:
    try:
        steps = flash_mod.flash_bootloader(
            args.project, board=args.board, output=args.output, dfu_util=args.dfu_util,
            sdk_home=_sdk_home(args), serial=args.serial, dry_run=args.dry_run)
    except FlashError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    return _report(args, steps)

"""CLI handlers for the ``openmv-ota flash`` command group.

    firmware   flash the firmware image (``--coprocessor`` also flashes the AE3 HE core)
    romfs      flash the app romfs image
    factory    flash the manufacturing program: firmware + the dual-slot factory image

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
    p.add_argument("--sdk-home", help="SDK home to find dfu-util under (<home>/bin/dfu-util)")
    p.add_argument("--no-reset", dest="reset", action="store_false",
                   help="don't reset (reboot) the board after flashing")
    p.add_argument("--dry-run", action="store_true",
                   help="print the dfu-util commands without running them")


def register(flash_parser: argparse.ArgumentParser):
    sub = flash_parser.add_subparsers(dest="_subcommand")

    p_fw = sub.add_parser("firmware", help="flash the firmware image")
    _add_common(p_fw)
    p_fw.add_argument("--coprocessor", action="store_true",
                      help="also flash the coprocessor core (AE3)")
    p_fw.set_defaults(func=cmd_firmware, _command="flash firmware")

    p_ro = sub.add_parser("romfs", help="flash the app romfs image")
    _add_common(p_ro)
    p_ro.set_defaults(func=cmd_romfs, _command="flash romfs")

    p_fa = sub.add_parser("factory", help="flash firmware + the dual-slot factory image")
    _add_common(p_fa)
    p_fa.add_argument("--coprocessor", action="store_true",
                      help="also flash the coprocessor core (AE3)")
    p_fa.set_defaults(func=cmd_factory, _command="flash factory")
    return sub


def _sdk_home(args: argparse.Namespace) -> Path | None:
    return Path(args.sdk_home) if args.sdk_home else None


def _report(args: argparse.Namespace, steps) -> int:
    for s in steps:
        if args.dry_run:
            print("would run: %s" % " ".join(s.argv))
        else:
            print("Flashed %s -> alt %d (%s)" % (s.file.name, s.alt, args.board))
    return 0


def cmd_firmware(args: argparse.Namespace) -> int:
    try:
        steps = flash_mod.flash_firmware(
            args.project, board=args.board, output=args.output, dfu_util=args.dfu_util,
            sdk_home=_sdk_home(args), coprocessor=args.coprocessor, reset=args.reset,
            dry_run=args.dry_run)
    except FlashError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    return _report(args, steps)


def cmd_romfs(args: argparse.Namespace) -> int:
    try:
        steps = flash_mod.flash_romfs(
            args.project, board=args.board, output=args.output, dfu_util=args.dfu_util,
            sdk_home=_sdk_home(args), reset=args.reset, dry_run=args.dry_run)
    except FlashError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    return _report(args, steps)


def cmd_factory(args: argparse.Namespace) -> int:
    try:
        steps = flash_mod.flash_factory(
            args.project, board=args.board, output=args.output, dfu_util=args.dfu_util,
            sdk_home=_sdk_home(args), coprocessor=args.coprocessor, reset=args.reset,
            dry_run=args.dry_run)
    except FlashError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    return _report(args, steps)

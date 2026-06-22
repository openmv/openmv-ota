"""CLI handlers for the ``openmv-ota build`` command group.

    romfs      compile + pack a romfs image from a project
    firmware   build firmware.bin (reserved; not implemented)

Note: ``build romfs`` (firmware-aware, compiles from a project) is distinct from
``romfs pack`` (low-level, packs a directory verbatim).
"""

from __future__ import annotations

import argparse
import sys

from . import romfs as build_mod
from .errors import BuildError


def register(build_parser: argparse.ArgumentParser):
    sub = build_parser.add_subparsers(dest="_subcommand")

    p = sub.add_parser("romfs", help="compile a project's app and pack a romfs image")
    p.add_argument("project", nargs="?", default=".", help="project directory (default: .)")
    p.add_argument("--app", help="app source dir (default: <project>/app)")
    p.add_argument("-o", "--output", help="output dir (default: <project>/build)")
    p.add_argument("-b", "--board", action="append", metavar="NAME",
                   help="only build this board (repeatable; default: all targets)")
    p.add_argument("-p", "--partition", type=int, help="only build this partition")
    p.add_argument("--no-compile-py", dest="compile_py", action="store_false",
                   help="pack .py as source (skip mpy-cross)")
    p.add_argument("--no-convert-models", dest="convert_models", action="store_false",
                   help="pack models as-is (skip vela/stedgeai)")
    p.add_argument("--mpy-arg", action="append", default=[], metavar="ARG",
                   help="extra mpy-cross arg (repeatable)")
    p.add_argument("--vela-arg", action="append", default=[], metavar="ARG",
                   help="extra vela arg (repeatable)")
    p.add_argument("--stedgeai-arg", action="append", default=[], metavar="ARG",
                   help="extra stedgeai arg (repeatable)")
    p.add_argument("--vela-optimise", choices=["Performance", "Size"], default="Performance",
                   help="vela optimisation (default: Performance)")
    p.add_argument("--stedgeai-optimization", type=int, choices=[0, 1, 2, 3], default=3,
                   help="st edge ai level (default: 3 = max)")
    p.add_argument("-f", "--firmware", help="firmware checkout override")
    p.add_argument("--allow-oversize", action="store_true",
                   help="warn instead of failing when an image exceeds the partition")
    p.add_argument("--keep-build-dir", action="store_true",
                   help="keep the staging dir for inspection")
    p.set_defaults(func=cmd_romfs, _command="build romfs")

    p_fw = sub.add_parser("firmware", help="build firmware.bin (not implemented yet)")
    p_fw.set_defaults(func=cmd_firmware, _command="build firmware")
    return sub


def cmd_romfs(args: argparse.Namespace) -> int:
    try:
        results = build_mod.build_romfs(
            args.project, app=args.app, output=args.output, boards=args.board,
            partition=args.partition, compile_py=args.compile_py,
            convert_models=args.convert_models, mpy_extra=args.mpy_arg,
            vela_extra=args.vela_arg, stedgeai_extra=args.stedgeai_arg,
            vela_optimise=args.vela_optimise,
            stedgeai_optimization=args.stedgeai_optimization, firmware=args.firmware,
            allow_oversize=args.allow_oversize, keep_build_dir=args.keep_build_dir,
        )
    except BuildError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code

    for r in results:
        pct = (r.size / r.capacity * 100) if r.capacity else 0
        print("Built %s  (%d bytes, %.1f%% of partition)" % (r.output, r.size, pct))
        if r.build_dir is not None:
            print("  build dir kept: %s" % r.build_dir)
    return 0


def cmd_firmware(args: argparse.Namespace) -> int:
    print("build firmware: not implemented yet", file=sys.stderr)
    return 2

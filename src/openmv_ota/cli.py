"""Single CLI entry point for openmv-ota.

Commands are namespaced by subsystem:

    openmv-ota romfs …      pack / inspect a ROMFS image from a directory
    openmv-ota project …    peg a project to a firmware checkout + toolchain
    openmv-ota build romfs  compile a project's app + pack a romfs image

    (future) openmv-ota build firmware   build firmware.bin
    (future) openmv-ota ota …            signing / slots / update server
"""

from __future__ import annotations

import argparse
import sys


def _not_implemented(args: argparse.Namespace) -> int:
    print(f"openmv-ota: '{args._command}' is not implemented yet.", file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openmv-ota", description=__doc__)
    parser.add_argument("--version", action="store_true", help="print version and exit")
    sub = parser.add_subparsers(dest="_command")

    p_init = sub.add_parser("init", help="scaffold a customer repo layout")
    p_init.set_defaults(func=_not_implemented, _command="init")

    p_keys = sub.add_parser("keys", help="key management")
    keys_sub = p_keys.add_subparsers(dest="_subcommand")
    p_keys_gen = keys_sub.add_parser("generate", help="create trusted_keys.json + keys")
    p_keys_gen.set_defaults(func=_not_implemented, _command="keys generate")

    p_romfs = sub.add_parser("romfs", help="ROMFS image tool (pack/inspect a directory)")
    from openmv_ota.romfs import cli as romfs_cli

    romfs_cli.register(p_romfs)

    p_project = sub.add_parser("project", help="create/manage a firmware-pegged project")
    from openmv_ota.project import cli as project_cli

    project_cli.register(p_project)

    p_build = sub.add_parser("build", help="firmware-aware artifact builders (romfs, …)")
    from openmv_ota.build import cli as build_cli

    build_cli.register(p_build)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "version", False):
        from openmv_ota import __version__

        print(__version__)
        return 0

    if not getattr(args, "func", None):
        parser.print_help()
        return 1

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

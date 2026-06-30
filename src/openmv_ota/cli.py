"""Single CLI entry point for openmv-ota.

Commands are namespaced by subsystem:

    openmv-ota project …    peg a project to a firmware checkout + toolchain
    openmv-ota build …      compile + sign romfs / factory / firmware images
    openmv-ota flash …      flash built artifacts onto a board (dfu-util)
    openmv-ota romfs …      low-level pack / inspect of a ROMFS image directory
"""

from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openmv-ota", description=__doc__)
    parser.add_argument("--version", action="store_true", help="print version and exit")
    sub = parser.add_subparsers(dest="_command")

    # OTA signing keys are provisioned by `project new --ota` and managed via
    # `project keys` (status / rotate / revoke / unrevoke).

    p_romfs = sub.add_parser("romfs", help="ROMFS image tool (pack/inspect a directory)")
    from openmv_ota.romfs import cli as romfs_cli

    romfs_cli.register(p_romfs)

    p_project = sub.add_parser("project", help="create/manage a firmware-pegged project")
    from openmv_ota.project import cli as project_cli

    project_cli.register(p_project)

    p_build = sub.add_parser("build", help="firmware-aware artifact builders (romfs, …)")
    from openmv_ota.build import cli as build_cli

    build_cli.register(p_build)

    p_flash = sub.add_parser("flash", help="flash built artifacts onto a board (dfu-util)")
    from openmv_ota.flash import cli as flash_cli

    flash_cli.register(p_flash)

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

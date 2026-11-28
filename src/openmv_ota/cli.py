"""Single CLI entry point for openmv-ota.

Commands are namespaced by subsystem:

    openmv-ota project …    peg a project to a firmware checkout + toolchain
    openmv-ota build …      compile + sign romfs / factory / firmware images
    openmv-ota flash …      flash built artifacts onto a board (dfu-util)
    openmv-ota romfs …      low-level pack / inspect of a ROMFS image directory
    openmv-ota server …     run/manage the update server (needs the `server` extra)
    openmv-ota client …     talk to an update server's admin API (publish, rollouts, fleet)
"""

from __future__ import annotations

import argparse
import os
import sys


def build_parser() -> argparse.ArgumentParser:
    # RawDescriptionHelpFormatter: the module docstring above is an aligned LIST of subsystems,
    # and argparse's default formatter re-wraps it into one run-on paragraph -- which is how
    # `openmv-ota --help` came to open with an unreadable wall of text.
    parser = argparse.ArgumentParser(prog="openmv-ota", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
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

    p_server = sub.add_parser("server", help="run/manage the update server (needs [server] extra)")
    from openmv_ota.server import cli as server_cli

    server_cli.register(p_server)

    p_keykeeper = sub.add_parser("keykeeper", help="run the private per-camera key service "
                                 "(needs [server] extra)")
    from openmv_ota.keykeeper import cli as keykeeper_cli

    keykeeper_cli.register(p_keykeeper)

    p_client = sub.add_parser("client", help="talk to an update server's admin API")
    from openmv_ota.client import cli as client_cli

    client_cli.register(p_client)

    return parser


def _drop_cwd_from_path() -> None:
    """Started as ``python -m openmv_ota.cli`` the working directory is first on the import
    path, and every import made later (a signer's SDK, mpy_cross) would take a file of that
    name from a project someone else made. Dropped, unless this package itself is run from
    there. This module's own imports came before it: ``python -m openmv_ota`` (``__main__``)
    drops the directory before any of them."""
    here = os.getcwd()
    if sys.path and sys.path[0] in ("", here) and \
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))) != here:
        del sys.path[0]


def main(argv: list[str] | None = None) -> int:
    _drop_cwd_from_path()
    from openmv_ota.terminal import guard

    guard()                    # what a server, camera or USB device wrote shows inert on a terminal
    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "version", False):
        from openmv_ota import __version__

        print(__version__)
        return 0

    if not getattr(args, "func", None):
        parser.print_help()
        return 1

    # A command that names a project checks it before it does anything else: every one of
    # them reads or writes the project's own files somewhere, and each used to have to
    # remember to (see project/outdir.check_project).
    from openmv_ota.project.config import CONFIG_NAME
    from openmv_ota.project.errors import ProjectError
    from openmv_ota.project.outdir import check_project
    # ...and so does one run from inside a project: the commands that take no project write
    # their output where they are run (`client release manifest`, `artifact`, `bases`).
    for root in [getattr(args, name, None) for name in ("dir", "project")] + ["."]:
        if isinstance(root, str) and os.path.lexists(os.path.join(root, CONFIG_NAME)):
            try:
                check_project(root)
            except ProjectError as e:
                print("error: %s" % e, file=sys.stderr)
                return e.exit_code

    try:
        return args.func(args)
    except OSError as e:
        # The filesystem said no where a command did not look first (`build` a file where a
        # directory is made, a directory where a file is written): one line, like every other
        # error, not a traceback. A closed pipe is the reader's choice, not an error.
        if isinstance(e, BrokenPipeError):
            return 1
        print("error: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

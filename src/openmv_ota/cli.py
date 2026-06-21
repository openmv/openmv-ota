"""Single CLI entry point for openmv-ota.

Commands are namespaced by subsystem so the tool can grow beyond ROMFS OTA
without renaming anything:

    openmv-ota init                     scaffold a customer repo layout
    openmv-ota keys generate            create trusted_keys.json + signing keys
    openmv-ota romfs build-firmware     Tool 1 — firmware w/ frozen boot.py
    openmv-ota romfs build --mode ...   Tool 3 — compose factory / OTA images
    openmv-ota romfs serve              Tool 4 — update server (local dev)
    openmv-ota romfs publish            upload a signed OTA release

    (future) openmv-ota firmware ...    whole-firmware OTA
    (future) openmv-ota bootloader ...  bootloader OTA

Subcommands are stubs today; see the concept plan for intended behaviour.
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

    p_romfs = sub.add_parser("romfs", help="ROMFS image + OTA subsystem")
    from openmv_ota.romfs import cli as romfs_cli

    romfs_sub = romfs_cli.register(p_romfs)
    # OTA-layer subcommands (not implemented yet; the core builder is `romfs build`).
    for name, help_text in (
        ("build-firmware", "build openmv firmware with frozen boot.py + ed25519_verify"),
        ("pack", "compose a signed factory or OTA ROMFS slot"),
        ("serve", "run the update server (local dev)"),
        ("publish", "upload a signed OTA release"),
    ):
        sp = romfs_sub.add_parser(name, help=help_text)
        sp.set_defaults(func=_not_implemented, _command=f"romfs {name}")

    p_project = sub.add_parser("project", help="create/manage a firmware-pegged OTA project")
    from openmv_ota.project import cli as project_cli

    project_cli.register(p_project)

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

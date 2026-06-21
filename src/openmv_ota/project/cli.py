"""CLI handlers for the ``openmv-ota project`` command group.

    new      peg a project to a local firmware checkout
    setup    reconstruct the pinned checkout + SDK from the committed lock
    show     print the resolved snapshot
    status   check the lock against the current checkout (drift)
    sync     re-resolve and rewrite the lock
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import lock as lock_mod
from . import project as proj
from .errors import ProjectError


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _path(value: str | None) -> Path | None:
    return Path(value) if value else None


def register(project_parser: argparse.ArgumentParser):
    sub = project_parser.add_subparsers(dest="_subcommand")

    p_new = sub.add_parser("new", help="peg a project to a local firmware checkout")
    p_new.add_argument("dir", nargs="?", default=".", help="project directory (default: .)")
    p_new.add_argument("-f", "--firmware", required=True, help="local OpenMV checkout path")
    p_new.add_argument("-b", "--board", action="append", required=True, metavar="NAME",
                       help="target board (repeatable)")
    p_new.add_argument("--product", help="product name (defaults to the directory name)")
    p_new.add_argument("--vendor", help="vendor name")
    p_new.add_argument("--sdk-home", help="SDK install dir (default ~/openmv-sdk-<SDK_VERSION>)")
    p_new.add_argument("--install-sdk", action="store_true", help="run `make sdk` if missing")
    p_new.add_argument("--allow-dirty", action="store_true", help="don't warn on a dirty checkout")
    p_new.add_argument("--force", action="store_true", help="overwrite an existing project")
    p_new.set_defaults(func=cmd_new, _command="project new")

    p_setup = sub.add_parser("setup", help="reconstruct the pinned checkout + SDK")
    p_setup.add_argument("dir", nargs="?", default=".", help="project directory (default: .)")
    p_setup.add_argument("--cache", help="clone cache dir (default: $OPENMV_OTA_CACHE / platform)")
    p_setup.add_argument("--sdk-home", help="SDK install dir override")
    p_setup.add_argument("--no-install-sdk", dest="install_sdk", action="store_false",
                         default=True, help="don't run `make sdk` after cloning")
    p_setup.set_defaults(func=cmd_setup, _command="project setup")

    p_show = sub.add_parser("show", help="print the resolved snapshot")
    p_show.add_argument("dir", nargs="?", default=".", help="project directory (default: .)")
    p_show.add_argument("--json", action="store_true", help="dump the raw lock JSON")
    p_show.set_defaults(func=cmd_show, _command="project show")

    p_status = sub.add_parser("status", help="check the lock against the current checkout")
    p_status.add_argument("dir", nargs="?", default=".", help="project directory (default: .)")
    p_status.add_argument("-f", "--firmware", help="checkout path override")
    p_status.add_argument("-q", "--quiet", action="store_true", help="exit code only")
    p_status.set_defaults(func=cmd_status, _command="project status")

    p_sync = sub.add_parser("sync", help="re-resolve and rewrite the lock")
    p_sync.add_argument("dir", nargs="?", default=".", help="project directory (default: .)")
    p_sync.add_argument("-f", "--firmware", help="checkout path override")
    p_sync.add_argument("--sdk-home", help="SDK install dir override")
    p_sync.add_argument("--install-sdk", action="store_true", help="run `make sdk` if missing")
    p_sync.add_argument("--allow-dirty", action="store_true", help="don't warn on a dirty checkout")
    p_sync.set_defaults(func=cmd_sync, _command="project sync")

    return sub


def _warn(warnings: list[str]) -> None:
    for w in warnings:
        print("warning: %s" % w, file=sys.stderr)


def cmd_new(args: argparse.Namespace) -> int:
    try:
        lock, warnings = proj.create_project(
            Path(args.dir),
            firmware=Path(args.firmware),
            boards=args.board,
            product=args.product,
            vendor=args.vendor,
            sdk_home_override=_path(args.sdk_home),
            install_sdk=args.install_sdk,
            allow_dirty=args.allow_dirty,
            force=args.force,
            now=_now(),
        )
    except ProjectError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    _warn(warnings)
    print("Created project in %s" % args.dir)
    _print_summary(lock)
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    try:
        repo = proj.setup_project(
            Path(args.dir),
            cache_override=args.cache,
            sdk_home_override=_path(args.sdk_home),
            install_sdk=args.install_sdk,
        )
    except ProjectError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    print("Firmware ready at %s" % repo)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    paths = proj.ProjectPaths(Path(args.dir))
    try:
        locked = lock_mod.read(paths.lock)
    except ProjectError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    if args.json:
        print(json.dumps(locked.to_dict(), indent=2))
        return 0
    _print_summary(locked)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    try:
        changes = proj.status_project(Path(args.dir), firmware=_path(args.firmware), now=_now())
    except ProjectError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    if not changes:
        if not args.quiet:
            print("in sync")
        return 0
    if not args.quiet:
        print("drift detected:")
        for c in changes:
            print("  %s" % c)
    return 1


def cmd_sync(args: argparse.Namespace) -> int:
    try:
        lock, warnings = proj.sync_project(
            Path(args.dir),
            firmware=_path(args.firmware),
            sdk_home_override=_path(args.sdk_home),
            install_sdk=args.install_sdk,
            allow_dirty=args.allow_dirty,
            now=_now(),
        )
    except ProjectError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    _warn(warnings)
    print("Re-locked %s" % args.dir)
    _print_summary(lock)
    return 0


def _print_summary(lock: lock_mod.Lock) -> None:
    fw = lock.firmware
    mp = lock.micropython
    tc = lock.toolchain
    dirty = " (dirty)" if fw.get("dirty") else ""
    branch = fw.get("branch") or "detached"
    print("  firmware:    %s  commit %s%s" % (fw.get("version"), (fw.get("commit") or "")[:12], dirty))
    print("               branch %s  describe %s" % (branch, fw.get("describe")))
    print("  micropython: %s  (.mpy abi %s.%s)"
          % (mp.get("version"), mp.get("mpy_abi_version"), mp.get("mpy_sub_version")))
    print("  sdk:         %s" % lock.sdk.get("version"))
    print("  toolchain:   mpy-cross %s, vela %s, stedgeai %s"
          % (tc.get("mpy_cross", {}).get("version"),
             tc.get("vela", {}).get("version"),
             tc.get("stedgeai", {}).get("version")))
    for name, rb in lock.targets.get("resolved", {}).items():
        print("  board %-18s part %s  front %s  (%s)"
              % (name, rb.get("partition_size"), rb.get("front_size"), rb.get("geometry_source")))

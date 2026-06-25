"""CLI handlers for the ``openmv-ota build`` command group.

    romfs      compile + pack a romfs image from a project
    firmware   build firmware per board (OTA projects freeze a boot.py)
    inspect    decode + print a signed OTA trailer
    verify     verify a built OTA image (signature + body hash)

Note: ``build romfs`` (firmware-aware, compiles from a project) is distinct from
``romfs pack`` (low-level, packs a directory verbatim).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

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
                   help="warn instead of failing when an image exceeds its capacity")
    p.add_argument("--keep-build-dir", action="store_true",
                   help="keep the staging dir for inspection")
    p.set_defaults(func=cmd_romfs, _command="build romfs")

    p_fw = sub.add_parser("firmware", help="build firmware per board (OTA projects freeze boot.py)")
    p_fw.add_argument("project", nargs="?", default=".", help="project directory (default: .)")
    p_fw.add_argument("-o", "--output", help="output dir (default: <project>/build)")
    p_fw.add_argument("-b", "--board", action="append", metavar="NAME",
                      help="only build this board (repeatable; default: all boards)")
    p_fw.add_argument("-j", "--jobs", type=int, metavar="N",
                      help="parallel make jobs (default: CPU count)")
    p_fw.add_argument("--incremental", action="store_true",
                      help="skip the clean rebuild (faster; only when the tree is known good)")
    p_fw.add_argument("-f", "--firmware", help="firmware checkout override")
    p_fw.add_argument("--keep-build-dir", action="store_true",
                      help="keep the generated wrapper manifest dir (OTA builds) for inspection")
    p_fw.set_defaults(func=cmd_firmware, _command="build firmware")

    p_ins = sub.add_parser("inspect", help="decode + print an OTA image's trailer")
    p_ins.add_argument("image", help="a <board>.zip bundle or a trailer.bin")
    p_ins.add_argument("--json", action="store_true", help="machine-readable dump")
    p_ins.set_defaults(func=cmd_inspect, _command="build inspect")

    p_ver = sub.add_parser("verify", help="verify an OTA image (signature + body hash)")
    p_ver.add_argument("image", help="a <board>.zip bundle, or the romfs.img body")
    p_ver.add_argument("trailer", nargs="?", help="trailer.bin (omit when image is a .zip)")
    p_ver.add_argument("--trusted-keys", default="keys/trusted_keys.json",
                       help="trusted_keys.json (default: keys/trusted_keys.json)")
    p_ver.set_defaults(func=cmd_verify, _command="build verify")

    p_fac = sub.add_parser("factory-romfs", help="compose the dual-slot factory ROMFS image")
    p_fac.add_argument("project", nargs="?", default=".", help="project directory (default: .)")
    p_fac.add_argument("--app", help="app source dir (default: <project>/app)")
    p_fac.add_argument("-o", "--output", help="output dir (default: <project>/build)")
    p_fac.add_argument("-b", "--board", action="append", metavar="NAME",
                       help="only build this board (repeatable; default: all targets)")
    p_fac.add_argument("--no-compile-py", dest="compile_py", action="store_false",
                       help="pack .py as source (skip mpy-cross)")
    p_fac.add_argument("--no-convert-models", dest="convert_models", action="store_false",
                       help="pack models as-is (skip vela/stedgeai)")
    p_fac.add_argument("--mpy-arg", action="append", default=[], metavar="ARG",
                       help="extra mpy-cross arg (repeatable)")
    p_fac.add_argument("--vela-arg", action="append", default=[], metavar="ARG",
                       help="extra vela arg (repeatable)")
    p_fac.add_argument("--stedgeai-arg", action="append", default=[], metavar="ARG",
                       help="extra stedgeai arg (repeatable)")
    p_fac.add_argument("--vela-optimise", choices=["Performance", "Size"], default="Performance",
                       help="vela optimisation (default: Performance)")
    p_fac.add_argument("--stedgeai-optimization", type=int, choices=[0, 1, 2, 3], default=3,
                       help="st edge ai level (default: 3 = max)")
    p_fac.add_argument("-f", "--firmware", help="firmware checkout override")
    p_fac.add_argument("--factory-key", type=lambda s: int(s, 0), metavar="ID",
                       help="factory key id to sign with (default 0x0001)")
    p_fac.add_argument("--keep-build-dir", action="store_true",
                       help="keep the staging dir for inspection")
    p_fac.set_defaults(func=cmd_factory_romfs, _command="build factory-romfs")
    return sub


def cmd_romfs(args: argparse.Namespace) -> int:
    try:
        results = build_mod.build_romfs(
            args.project, app=args.app, output=args.output, boards=args.board,
            compile_py=args.compile_py,
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
        kind = "signed OTA bundle" if r.ota else "image"
        print("Built %s  (%s, %d-byte body, %.1f%% of %s)"
              % (r.output, kind, r.size, pct, r.bound))
        if r.build_dir is not None:
            print("  build dir kept: %s" % r.build_dir)
    return 0


def cmd_factory_romfs(args: argparse.Namespace) -> int:
    try:
        results = build_mod.build_factory_romfs(
            args.project, app=args.app, output=args.output, boards=args.board,
            compile_py=args.compile_py,
            convert_models=args.convert_models, mpy_extra=args.mpy_arg,
            vela_extra=args.vela_arg, stedgeai_extra=args.stedgeai_arg,
            vela_optimise=args.vela_optimise,
            stedgeai_optimization=args.stedgeai_optimization, firmware=args.firmware,
            factory_key=args.factory_key, keep_build_dir=args.keep_build_dir,
        )
    except BuildError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code

    for r in results:
        pct = (r.size / r.capacity * 100) if r.capacity else 0
        print("Built %s  (factory image, %d-byte body, %.1f%% of %s)"
              % (r.output, r.size, pct, r.bound))
        if r.build_dir is not None:
            print("  build dir kept: %s" % r.build_dir)
    return 0


def cmd_firmware(args: argparse.Namespace) -> int:
    from . import firmware as firmware_mod

    try:
        results = firmware_mod.build_firmware(
            args.project, output=args.output, boards=args.board, firmware=args.firmware,
            jobs=args.jobs, incremental=args.incremental, keep_build_dir=args.keep_build_dir,
        )
    except BuildError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code

    for r in results:
        kind = "OTA firmware" if r.ota else "firmware"
        print("Built %s  (%s)" % (", ".join(o.name for o in r.outputs), kind))
        if r.build_dir is not None:
            print("  wrapper dir kept: %s" % r.build_dir)
    return 0


def _trailer_summary(t) -> dict:
    """A flat, JSON-friendly view of a parsed trailer (versions decoded to semver)."""
    from openmv_ota.ota import algorithm_for
    from openmv_ota.ota.version import decode_app_version

    meta = t.meta if isinstance(t.meta, dict) else {}
    return {
        "kind": "ROMFS app",
        "header_version": t.header_version,
        "product": meta.get("product"),
        "board": meta.get("board"),
        "board_id": t.board_id,
        "board_name": meta.get("board_name"),
        "vendor": meta.get("vendor"),
        "app_version": meta.get("app_version"),
        "payload_version": decode_app_version(t.payload_version),
        "rollback_floor": decode_app_version(t.payload_version_floor) if t.payload_version_floor
        else "none",
        "min_platform_version": decode_app_version(t.min_platform_version)
        if t.min_platform_version else "none",
        "key_id": "0x%04x" % t.key_id,
        "sig_alg": algorithm_for(t.sig_alg).name,
        "body_size": t.body_size,
        "pad_size": t.pad_size,
        "body_sha256": t.body_sha256.hex(),
        "signature_size": len(t.signature),
        "meta": meta,
    }


def cmd_inspect(args: argparse.Namespace) -> int:
    from openmv_ota.ota import bundle, parse_trailer
    from openmv_ota.ota.errors import OtaError

    path = Path(args.image)
    try:
        if bundle.is_bundle(path):
            _body, trailer_bytes = bundle.read_bundle(path)
        else:
            trailer_bytes = path.read_bytes()
    except (OSError, OtaError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    try:
        t = parse_trailer(trailer_bytes)
    except OtaError as e:
        print("error: not a valid trailer: %s" % e, file=sys.stderr)
        return 2

    s = _trailer_summary(t)
    if args.json:
        print(json.dumps(s, indent=2))
        return 0
    print("OTA trailer (%s, header v%d)" % (s["kind"], s["header_version"]))
    print("  product:        %s" % s["product"])
    print("  board:          %s  (id %d)" % (s["board"], s["board_id"]))
    print("  board_name:     %s" % s["board_name"])
    print("  app_version:    %s  (payload_version %s)" % (s["app_version"], s["payload_version"]))
    print("  rollback_floor: %s" % s["rollback_floor"])
    print("  min_platform:   %s" % s["min_platform_version"])
    print("  signed by:      key %s  (%s, %d-byte sig)"
          % (s["key_id"], s["sig_alg"], s["signature_size"]))
    print("  body:           %d bytes, sha256 %s" % (s["body_size"], s["body_sha256"]))
    print("  pad_size:       %d" % s["pad_size"])
    fw, tc = s["meta"].get("firmware", {}), s["meta"].get("toolchain", {})
    print("  provenance:     firmware %s (%s), micropython %s"
          % (fw.get("version"), (fw.get("commit") or "")[:12], s["meta"].get("micropython")))
    print("                  mpy-cross %s, vela %s, stedgeai %s, sdk %s"
          % (tc.get("mpy_cross"), tc.get("vela"), tc.get("stedgeai"), tc.get("sdk")))
    print("  (full meta: --json)")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    from openmv_ota.ota import bundle, read_trusted_keys
    from openmv_ota.ota.errors import OtaError
    from openmv_ota.ota.verify import verify_image

    image = Path(args.image)
    try:
        if args.trailer is not None:
            body, trailer = image.read_bytes(), Path(args.trailer).read_bytes()
        elif bundle.is_bundle(image):
            body, trailer = bundle.read_bundle(image)
        else:
            print("error: %s is not a .zip bundle; pass `<romfs.img> <trailer.bin>` or a "
                  "bundle" % image, file=sys.stderr)
            return 2
    except (OSError, OtaError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    try:
        trusted = read_trusted_keys(Path(args.trusted_keys))
    except OtaError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2

    ok, reason = verify_image(body, trailer, trusted)
    if ok:
        print("verified: %s" % reason)
        return 0
    print("verification FAILED: %s" % reason, file=sys.stderr)
    return 1

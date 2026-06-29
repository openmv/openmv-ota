"""CLI handlers for the ``openmv-ota build`` command group.

    romfs         compile + pack a romfs image from a project
    factory-romfs compose the dual-slot factory image (flashed at manufacture)
    firmware      build firmware per board (OTA projects freeze a boot.py)
    ota-romfs     build the cloud-published OTA set: image + signed manifest (+ delta)
    inspect       decode + print an OTA artifact (trailer, manifest, or delta)
    verify        verify an OTA artifact (image/manifest signature, or a delta)

Note: ``build romfs`` (firmware-aware, compiles from a project) is distinct from
``romfs pack`` (low-level, packs a directory verbatim).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from openmv_ota.project import history

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

    p_otr = sub.add_parser("ota-romfs",
                           help="build the cloud-published OTA set from app source: image + "
                                "signed manifest (+ optional delta)")
    p_otr.add_argument("project", nargs="?", default=".", help="project directory (default: .)")
    p_otr.add_argument("--delta-from", metavar="PATH",
                       help="the factory image (<board>-factory-romfs.img, or a dir of them) "
                            "to build a delta against the golden BACK slot")
    p_otr.add_argument("--app", help="app source dir (default: <project>/app)")
    p_otr.add_argument("-o", "--output", help="output dir (default: <project>/build)")
    p_otr.add_argument("-b", "--board", action="append", metavar="NAME",
                       help="only build this board (repeatable; default: all targets)")
    p_otr.add_argument("--no-compile-py", dest="compile_py", action="store_false",
                       help="pack .py as source (skip mpy-cross)")
    p_otr.add_argument("--no-convert-models", dest="convert_models", action="store_false",
                       help="pack models as-is (skip vela/stedgeai)")
    p_otr.add_argument("--mpy-arg", action="append", default=[], metavar="ARG",
                       help="extra mpy-cross arg (repeatable)")
    p_otr.add_argument("--vela-arg", action="append", default=[], metavar="ARG",
                       help="extra vela arg (repeatable)")
    p_otr.add_argument("--stedgeai-arg", action="append", default=[], metavar="ARG",
                       help="extra stedgeai arg (repeatable)")
    p_otr.add_argument("--vela-optimise", choices=["Performance", "Size"], default="Performance",
                       help="vela optimisation (default: Performance)")
    p_otr.add_argument("--stedgeai-optimization", type=int, choices=[0, 1, 2, 3], default=3,
                       help="st edge ai level (default: 3 = max)")
    p_otr.add_argument("--allow-republish", action="store_true",
                       help="allow a version <= the last published (re-sign / downgrade)")
    p_otr.add_argument("-f", "--firmware", help="firmware checkout override")
    p_otr.set_defaults(func=cmd_ota_romfs, _command="build ota-romfs")

    p_ins = sub.add_parser("inspect", help="decode + print an OTA artifact "
                                           "(image trailer, manifest, or delta)")
    p_ins.add_argument("image", help="a romfs.zip bundle, trailer.bin, factory/partition "
                                     ".img, a -manifest.bin, or a .delta.gz")
    p_ins.add_argument("--json", action="store_true", help="machine-readable dump")
    p_ins.set_defaults(func=cmd_inspect, _command="build inspect")

    p_ver = sub.add_parser("verify", help="verify an OTA artifact "
                                          "(image/manifest signature, or a delta)")
    p_ver.add_argument("image", help="a romfs.zip bundle, factory/partition .img, the "
                                     "romfs.img body, a -manifest.bin, or a .delta.gz")
    p_ver.add_argument("trailer", nargs="?", help="trailer.bin (omit when image is a .zip)")
    p_ver.add_argument("--trusted-keys", default="keys/trusted_keys.json",
                       help="trusted_keys.json (default: keys/trusted_keys.json)")
    p_ver.add_argument("--base", help="golden image to apply a delta against (delta verify)")
    p_ver.add_argument("--target", help="expected new image, to confirm a delta reconstructs it")
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
    history.record(args.project, "build-romfs",
                   outputs=[{"board": r.target, "file": r.output.name} for r in results])
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
    history.record(args.project, "build-factory-romfs",
                   outputs=[{"board": r.target, "file": r.output.name} for r in results])
    return 0


def cmd_ota_romfs(args: argparse.Namespace) -> int:
    try:
        results = build_mod.build_ota_romfs(
            args.project, delta_from=args.delta_from, app=args.app,
            output=args.output, boards=args.board, compile_py=args.compile_py,
            convert_models=args.convert_models, mpy_extra=args.mpy_arg,
            vela_extra=args.vela_arg, stedgeai_extra=args.stedgeai_arg,
            vela_optimise=args.vela_optimise,
            stedgeai_optimization=args.stedgeai_optimization, firmware=args.firmware,
            allow_republish=args.allow_republish,
        )
    except BuildError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code

    for r in results:
        extra = (" + %s" % r.delta.name) if r.delta else ""
        print("Built %s%s + %s  (OTA set, key 0x%04x)"
              % (r.image.name, extra, r.manifest.name, r.key_id))
    history.record(args.project, "build-ota-romfs", sets=[
        {"board": r.target, "image": r.image.name, "manifest": r.manifest.name,
         "delta": (r.delta.name if r.delta else None), "key_id": r.key_id}
        for r in results])
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


def _romfs_summary(data: bytes) -> str | None:
    """If ``data`` is a valid (unsigned) ROMFS image, a one-line description; else
    ``None``. Lets inspect/verify handle a plain ``<board>-romfs.img`` (or a
    coprocessor image) gracefully instead of calling it a bad trailer."""
    from openmv_ota.romfs.builder import read_image
    from openmv_ota.romfs.container import RomfsError

    try:
        reader = read_image(data)
        files = sum(1 for _, e in reader.walk() if not e.is_dir)
    except RomfsError:
        return None
    return ("unsigned ROMFS image: %d file(s), %d bytes, no OTA trailer "
            "(use `openmv-ota romfs inspect/ls` to inspect its contents)" % (files, len(data)))


def _print_trailer(s: dict) -> None:
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


def _load_artifact(path: Path):
    """Peek at a file and classify it for inspect/verify: ``("manifest", bytes)``,
    ``("delta", raw-patch-bytes)`` (gunzipping a ``.delta.gz``), or ``(None, None)`` to let
    the trailer/image path handle it. Never raises on a read error -- returns ``(None, None)``."""
    try:
        data = path.read_bytes()
    except OSError:
        return None, None
    if data[:4] == b"OMVM":
        return "manifest", data
    if data[:4] == b"OCDL":
        return "delta", data
    if data[:2] == b"\x1f\x8b":                            # gzip -- maybe a gzipped delta
        import gzip
        try:
            inner = gzip.decompress(data)
        except (OSError, EOFError):
            return None, None
        if inner[:4] == b"OCDL":
            return "delta", inner
    return None, None


def _inspect_manifest(raw: bytes, as_json: bool) -> int:
    from openmv_ota.ota.errors import OtaError
    from openmv_ota.ota.manifest import parse_manifest
    try:
        m = parse_manifest(raw)
    except OtaError as e:
        print("error: not a valid manifest: %s" % e, file=sys.stderr)
        return 2
    b = m.body
    if as_json:
        print(json.dumps({"key_id": m.key_id, "sig_alg": m.sig_alg, "body": b}, indent=2))
        return 0
    print("manifest  (signed by key 0x%04x, alg %d)" % (m.key_id, m.sig_alg))
    print("  board_id %s  version %s  (payload_version %s)"
          % (b.get("board_id"), b.get("version"), b.get("payload_version")))
    print("  image %s bytes  sha256 %s" % (b.get("size"), b.get("sha256")))
    for r in b.get("representations", []):
        base = ("  base_payload_version=%s" % r["base_payload_version"]
                if "base_payload_version" in r else "")
        print("  - %-5s %s bytes  %s%s"
              % (r.get("format"), r.get("size"), r.get("url"), base))
    return 0


def _inspect_delta(raw: bytes, as_json: bool) -> int:
    from openmv_ota.ota.delta import summarize
    from openmv_ota.ota.errors import OtaError
    try:
        s = summarize(raw)
    except OtaError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    if as_json:
        print(json.dumps(s, indent=2))
        return 0
    print("delta  (reconstructs %d bytes in %d ops)" % (s["target_size"], s["ops"]))
    print("  literal (extra): %d bytes" % s["extra_bytes"])
    print("  copy-with-diff:  %d bytes (%d changed)"
          % (s["diff_bytes"], s["nonzero_diff_bytes"]))
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    from openmv_ota.ota import bundle, parse_trailer, partition
    from openmv_ota.ota.errors import OtaError

    path = Path(args.image)
    kind, raw = _load_artifact(path)
    if kind == "manifest":
        return _inspect_manifest(raw, args.json)
    if kind == "delta":
        return _inspect_delta(raw, args.json)
    try:
        is_b = bundle.is_bundle(path)
        trailer_bytes = bundle.read_bundle(path)[1] if is_b else None
        data = None if is_b else path.read_bytes()
    except (OSError, OtaError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    # A factory/partition image has FRONT + BACK trailers; a bundle/trailer.bin has
    # one; a plain romfs has none -- report that gracefully rather than as an error.
    found = [] if is_b else partition.find_trailers(data)
    if not is_b and not found:
        summary = _romfs_summary(data)   # a plain, unsigned romfs (no trailer to decode)
        if summary is not None:
            print("%s\n  %s" % (args.image, summary))
            return 0
    try:
        if is_b:
            entries = [("image", parse_trailer(trailer_bytes))]
        elif found:
            entries = [(lbl, t) for lbl, (_off, t) in
                       zip(partition.slot_labels(len(found)), found)]
        else:
            entries = [("image", parse_trailer(data))]   # a bare trailer.bin, else raises
    except OtaError as e:
        print("error: not a valid OTA trailer, image, or ROMFS image: %s" % e, file=sys.stderr)
        return 2

    if args.json:
        out = ({lbl: _trailer_summary(t) for lbl, t in entries}
               if len(entries) > 1 else _trailer_summary(entries[0][1]))
        print(json.dumps(out, indent=2))
        return 0
    for lbl, t in entries:
        if len(entries) > 1:
            print("== %s slot ==" % lbl)
        _print_trailer(_trailer_summary(t))
    print("  (full meta: --json)")
    return 0


def _verify_delta(raw: bytes, args: argparse.Namespace) -> int:
    import hashlib

    from openmv_ota.ota.delta import apply_delta
    from openmv_ota.ota.errors import OtaError
    if not args.base:
        print("error: verifying a delta needs --base <golden image>", file=sys.stderr)
        return 2
    try:
        base = build_mod._read_maybe_gz(Path(args.base))
        recon = apply_delta(base, raw)
    except (OSError, OtaError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    sha = hashlib.sha256(recon).hexdigest()
    if args.target:
        target = build_mod._read_maybe_gz(Path(args.target))
        if recon != target:
            print("verification FAILED: delta does not reconstruct the target",
                  file=sys.stderr)
            return 1
        print("verified: delta reconstructs the target (%d bytes, sha256 %s)"
              % (len(recon), sha))
        return 0
    print("verified: delta applies against the base (%d bytes, sha256 %s)"
          % (len(recon), sha))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    from openmv_ota.ota import bundle, partition, read_trusted_keys
    from openmv_ota.ota.errors import OtaError
    from openmv_ota.ota.verify import verify_image, verify_manifest

    image = Path(args.image)
    kind, raw = _load_artifact(image)
    if kind == "delta":
        return _verify_delta(raw, args)
    try:
        trusted = read_trusted_keys(Path(args.trusted_keys))
    except OtaError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    if kind == "manifest":
        ok, reason = verify_manifest(raw, trusted)
        if ok:
            print("verified: %s" % reason)
            return 0
        print("verification FAILED: %s" % reason, file=sys.stderr)
        return 1
    try:
        if args.trailer is not None:                       # loose body + trailer
            pairs = [("image", image.read_bytes(), Path(args.trailer).read_bytes())]
        elif bundle.is_bundle(image):                      # one .zip bundle
            body, trailer = bundle.read_bundle(image)
            pairs = [("image", body, trailer)]
        else:                                              # a factory/partition .img
            data = image.read_bytes()
            pairs = partition.slots(data)
            if not pairs:
                if _romfs_summary(data) is not None:       # a plain, unsigned romfs
                    print("error: %s is an unsigned ROMFS image -- it has no trailer to "
                          "verify" % image, file=sys.stderr)
                else:
                    print("error: %s is not a .zip bundle, a factory/partition image, or a "
                          "signed body; pass `<romfs.img> <trailer.bin>` or a bundle"
                          % image, file=sys.stderr)
                return 2
    except (OSError, OtaError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2

    all_ok = True
    for label, body, trailer in pairs:
        tag = ("%s: " % label) if len(pairs) > 1 else ""
        ok, reason = verify_image(body, trailer, trusted)
        if ok:
            print("%sverified: %s" % (tag, reason))
        else:
            print("%sverification FAILED: %s" % (tag, reason), file=sys.stderr)
            all_ok = False
    return 0 if all_ok else 1

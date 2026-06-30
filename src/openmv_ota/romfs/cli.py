"""CLI handlers for the ``openmv-ota romfs`` command group (core builder).

Subcommands:
    pack      pack a directory into a ROMFS image, verbatim (board-aware alignment)
    unpack    unpack a ROMFS image to a directory
    ls        list the contents of a ROMFS image
    cat       write one file's contents from a ROMFS image to stdout
    inspect   summarise a ROMFS image
    verify    check an image parses and its payloads are correctly aligned
    boards    list known boards / show one board's ROMFS config

Images can be read from stdin and written to stdout with ``-`` as the path.
``--board`` supplies the defaults (alignment rules + capacity); per-extension
``--align`` flags override those defaults on top.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import boards as boards_mod
from . import builder as builder_mod
from .container import RomfsError, complete_suffix


# --- argument helpers -------------------------------------------------------

def _parse_size(text: str) -> int:
    """Parse a byte count: plain int, 0x hex, or a K/M/G suffix (1024-based)."""
    s = text.strip().lower()
    mult = 1
    if s and s[-1] in "kmg":
        mult = {"k": 1024, "m": 1024**2, "g": 1024**3}[s[-1]]
        s = s[:-1]
    base = 16 if s.startswith("0x") else 10
    try:
        return int(s, base) * mult
    except ValueError:
        raise argparse.ArgumentTypeError("invalid size %r" % text)


def _parse_align(value: str) -> dict[str, object]:
    """Parse an ``EXT=N`` (or ``EXT:N``) alignment rule."""
    for sep in ("=", ":"):
        if sep in value:
            ext, _, num = value.partition(sep)
            ext = ext.strip().lstrip(".").lower()
            if not ext:
                raise argparse.ArgumentTypeError("empty extension in --align %r" % value)
            try:
                alignment = int(num, 0)
            except ValueError:
                raise argparse.ArgumentTypeError("bad alignment in --align %r" % value)
            if alignment < 1 or (alignment & (alignment - 1)):
                raise argparse.ArgumentTypeError(
                    "--align %r: alignment must be a power of two" % value
                )
            return {"extension": ext, "alignment": alignment}
    raise argparse.ArgumentTypeError("expected EXT=N, got %r" % value)


def _human(n: int) -> str:
    units = ["B", "KiB", "MiB", "GiB"]
    size = float(n)
    for u in units:
        if size < 1024 or u == units[-1]:
            return ("%d %s" % (n, u)) if u == "B" else ("%.2f %s" % (size, u))
        size /= 1024
    return "%d B" % n  # pragma: no cover - unreachable (GiB is the catch-all)


def _read_image_bytes(path: str) -> bytes:
    """Read an image from a file, or from stdin when ``path`` is ``-``."""
    if path == "-":
        return sys.stdin.buffer.read()
    with open(path, "rb") as f:
        return f.read()


def _open_reader(path: str):
    """Return ``(reader, None)`` or ``(None, exit_code)`` after printing errors."""
    try:
        data = _read_image_bytes(path)
    except OSError as e:
        print("error: %s" % e, file=sys.stderr)
        return None, None, 2
    try:
        return builder_mod.read_image(data), data, 0
    except RomfsError as e:
        print("error: not a valid ROMFS image: %s" % e, file=sys.stderr)
        return None, data, 1


# --- subcommand registration ------------------------------------------------

def register(romfs_parser: argparse.ArgumentParser):
    """Register the core ``romfs`` subcommands. Returns the subparsers action so
    callers can attach additional (OTA-layer) subcommands to the same group."""
    sub = romfs_parser.add_subparsers(dest="_subcommand")

    p_pack = sub.add_parser("pack", help="pack a directory into a ROMFS image (verbatim)")
    p_pack.add_argument("src", help="source directory; its contents become the ROMFS root")
    p_pack.add_argument("-o", "--output", required=True,
                         help="output .romfs image path ('-' for stdout)")
    p_pack.add_argument("-b", "--board", help="board name (supplies alignment rules + size)")
    p_pack.add_argument("-p", "--partition", type=int, default=None,
                         help="partition index for multi-partition boards (default: first)")
    p_pack.add_argument("--alignment", "--align", action="append", type=_parse_align,
                         default=[], metavar="EXT=N", dest="align",
                         help="override the alignment for a file extension, on top of the "
                              "board defaults (repeatable), e.g. --align tflite=32")
    p_pack.add_argument("--default-alignment", type=int, default=None, metavar="N",
                         help="fallback alignment for extensions with no rule (default: 4)")
    p_pack.add_argument("--no-board-rules", action="store_true",
                         help="ignore the board's alignment defaults; use only --align")
    p_pack.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                         help="skip entries whose name matches GLOB (repeatable)")
    p_pack.add_argument("--no-default-excludes", action="store_true",
                         help="do not skip __pycache__/*.pyc/.git/.DS_Store/... by default")
    p_pack.add_argument("--follow-symlinks", action="store_true",
                         help="follow symlinks instead of skipping them")
    p_pack.add_argument("--max-size", type=_parse_size, default=None, metavar="BYTES",
                         help="capacity to check against (default: the board partition size); "
                              "accepts 0x.. or K/M/G suffixes")
    p_pack.add_argument("--allow-oversize", action="store_true",
                         help="warn instead of failing when the image exceeds capacity")
    p_pack.add_argument("-q", "--quiet", action="store_true", help="suppress the summary")
    p_pack.set_defaults(func=cmd_pack, _command="romfs pack")

    p_unpack = sub.add_parser("unpack", help="unpack a ROMFS image to a directory")
    p_unpack.add_argument("image", help="ROMFS image to unpack ('-' for stdin)")
    p_unpack.add_argument("-o", "--output", required=True, help="destination directory")
    p_unpack.add_argument("--force", action="store_true",
                           help="unpack even if the destination is not empty")
    p_unpack.set_defaults(func=cmd_unpack, _command="romfs unpack")

    p_ls = sub.add_parser("ls", help="list the contents of a ROMFS image")
    p_ls.add_argument("image", help="ROMFS image to list ('-' for stdin)")
    p_ls.add_argument("-l", "--long", action="store_true",
                      help="show size and offset/alignment for each file")
    p_ls.set_defaults(func=cmd_ls, _command="romfs ls")

    p_cat = sub.add_parser("cat", help="write one file's contents to stdout")
    p_cat.add_argument("image", help="ROMFS image ('-' for stdin)")
    p_cat.add_argument("path", help="path of the file inside the image")
    p_cat.set_defaults(func=cmd_cat, _command="romfs cat")

    p_info = sub.add_parser("inspect", help="summarise a ROMFS image")
    p_info.add_argument("image", help="ROMFS image to inspect ('-' for stdin)")
    p_info.set_defaults(func=cmd_inspect, _command="romfs inspect")

    p_verify = sub.add_parser("verify", help="check an image parses and is aligned")
    p_verify.add_argument("image", help="ROMFS image to verify ('-' for stdin)")
    p_verify.add_argument("-b", "--board", help="board whose alignment rules to check against")
    p_verify.add_argument("-p", "--partition", type=int, default=None, help="partition index")
    p_verify.add_argument("--alignment", "--align", action="append", type=_parse_align,
                          default=[], metavar="EXT=N", dest="align",
                          help="override an alignment rule for the check (repeatable)")
    p_verify.add_argument("--default-alignment", type=int, default=None, metavar="N",
                          help="fallback alignment for the check (default: 4)")
    p_verify.add_argument("--no-board-rules", action="store_true",
                          help="ignore the board's alignment rules")
    p_verify.set_defaults(func=cmd_verify, _command="romfs verify")

    p_boards = sub.add_parser("boards", help="list known boards or show one board's config")
    p_boards.add_argument("name", nargs="?", help="board to show in detail")
    p_boards.set_defaults(func=cmd_boards, _command="romfs boards")

    return sub


# --- shared rule resolution -------------------------------------------------

def _resolve_board(args):
    """Return ``(board, exit_code)``; board is None if --board not given."""
    if not args.board:
        return None, 0
    try:
        return boards_mod.get_board(args.board), 0
    except KeyError as e:
        print("error: %s" % e, file=sys.stderr)
        return None, 2


def _excludes(args) -> list[str]:
    patterns = [] if args.no_default_excludes else list(builder_mod.DEFAULT_EXCLUDES)
    patterns.extend(args.exclude)
    return patterns


# --- subcommand implementations ---------------------------------------------

def cmd_pack(args: argparse.Namespace) -> int:
    board, code = _resolve_board(args)
    if code:
        return code

    if board is None and args.no_board_rules:
        print("error: --no-board-rules needs a board; just omit --board instead",
              file=sys.stderr)
        return 2

    default_alignment = args.default_alignment if args.default_alignment is not None else 4
    if default_alignment < 1 or (default_alignment & (default_alignment - 1)):
        print("error: --default-alignment must be a power of two", file=sys.stderr)
        return 2

    excludes = _excludes(args)
    try:
        if board is not None:
            result = builder_mod.build_for_board(
                args.src, board,
                partition_index=args.partition,
                extra_rules=args.align,
                use_board_rules=not args.no_board_rules,
                default_alignment=default_alignment,
                exclude=excludes,
                follow_symlinks=args.follow_symlinks,
                max_size=args.max_size,
                allow_oversize=args.allow_oversize,
            )
        else:
            image = builder_mod.build_image(
                args.src, args.align, default_alignment=default_alignment,
                exclude=excludes, follow_symlinks=args.follow_symlinks,
            )
            result = builder_mod.BuildResult(image=image, partition=None,
                                             alignment_rules=args.align)
            if args.max_size is not None and len(image) > args.max_size \
                    and not args.allow_oversize:
                print("error: image is %s but --max-size is %s (%s over)"
                      % (_human(len(image)), _human(args.max_size),
                         _human(len(image) - args.max_size)), file=sys.stderr)
                return 1
    except builder_mod.BuildError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    except LookupError as e:  # bad --partition index
        print("error: %s" % e, file=sys.stderr)
        return 2

    if args.output == "-":
        sys.stdout.buffer.write(result.image)
    else:
        parent = os.path.dirname(os.path.abspath(args.output))
        os.makedirs(parent, exist_ok=True)
        with open(args.output, "wb") as f:
            f.write(result.image)

    eff_cap = args.max_size if args.max_size is not None else result.capacity
    if eff_cap and result.size > eff_cap and args.allow_oversize:
        print("warning: image exceeds the capacity by %s"
              % _human(result.size - eff_cap), file=sys.stderr)

    if not args.quiet and args.output != "-":
        _print_build_summary(board, result, args.output)
    return 0


def _print_build_summary(board, result, out) -> None:
    print("Wrote %s" % out)
    print("  size:       %s (%d bytes)" % (_human(result.size), result.size))
    if board is not None and result.partition is not None:
        p = result.partition
        pct = (result.size / p.size * 100) if p.size else 0
        print("  board:      %s (%s)" % (board.name, board.display_name))
        print("  partition:  [%d] %s - capacity %s" % (p.index, p.name, _human(p.size)))
        print("  usage:      %.1f%%  (%s free)" % (pct, _human(result.free or 0)))
    rules = result.alignment_rules
    if rules:
        print("  alignment:  " + ", ".join(
            "%s=%d" % (r["extension"], r["alignment"]) for r in rules))
    else:
        print("  alignment:  (defaults only)")


def cmd_unpack(args: argparse.Namespace) -> int:
    reader, _data, code = _open_reader(args.image)
    if reader is None:
        return code

    dest = args.output
    if os.path.isdir(dest) and os.listdir(dest) and not args.force:
        print("error: %s is not empty (use --force)" % dest, file=sys.stderr)
        return 1
    os.makedirs(dest, exist_ok=True)
    count = reader.extract(dest)
    print("Extracted %d file%s to %s" % (count, "" if count == 1 else "s", dest))
    return 0


def cmd_ls(args: argparse.Namespace) -> int:
    reader, _data, code = _open_reader(args.image)
    if reader is None:
        return code

    for path, entry in reader.walk():
        if entry.is_dir:
            print("%s/" % path if not args.long else "%-12s %s/" % ("<dir>", path))
        elif args.long:
            n = len(entry.data or b"")
            off = entry.data_offset if entry.data_offset is not None else -1
            suffix = complete_suffix(entry.name)
            print("%12d  off=%-9s sfx=%-6s %s"
                  % (n, off if off >= 0 else "?", suffix or "-", path))
        else:
            print(path)
    return 0


def cmd_cat(args: argparse.Namespace) -> int:
    reader, _data, code = _open_reader(args.image)
    if reader is None:
        return code

    target = args.path.strip("/")
    for path, entry in reader.walk():
        if path == target:
            if entry.is_dir:
                print("error: %s is a directory" % args.path, file=sys.stderr)
                return 1
            sys.stdout.buffer.write(entry.data or b"")
            return 0
    print("error: %s not found in image" % args.path, file=sys.stderr)
    return 1


def cmd_inspect(args: argparse.Namespace) -> int:
    reader, data, code = _open_reader(args.image)
    if reader is None:
        return code

    files = dirs = total = 0
    for _, entry in reader.walk():
        if entry.is_dir:
            dirs += 1
        else:
            files += 1
            total += len(entry.data or b"")
    name = "<stdin>" if args.image == "-" else args.image
    print("%s" % name)
    print("  image size:   %s (%d bytes)" % (_human(len(data)), len(data)))
    if reader.romfs_size != len(data):   # a trailer / slot pad / second slot follows
        print("  romfs size:   %s (%d bytes; %d trailing byte(s) ignored)"
              % (_human(reader.romfs_size), reader.romfs_size, len(data) - reader.romfs_size))
    print("  files:        %d  (payload %s)" % (files, _human(total)))
    print("  directories:  %d" % dirs)
    print("  magic:        OK (D2 CD 31)")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    board, code = _resolve_board(args)
    if code:
        return code
    if board is None and args.no_board_rules:
        print("error: --no-board-rules needs a board", file=sys.stderr)
        return 2

    try:
        data = _read_image_bytes(args.image)
    except OSError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2

    rules = list(args.align)
    if board is not None:
        try:
            partition = board.partition(args.partition)
        except LookupError as e:
            print("error: %s" % e, file=sys.stderr)
            return 2
        rules = builder_mod.resolve_rules(partition, args.align, not args.no_board_rules)

    default_alignment = args.default_alignment if args.default_alignment is not None else 4
    try:
        result = builder_mod.verify_image(data, rules, default_alignment)
    except RomfsError as e:
        print("FAIL: not a valid ROMFS image: %s" % e, file=sys.stderr)
        return 1

    if result.ok:
        print("OK: %d file%s, %d director%s, all payloads aligned"
              % (result.files, "" if result.files == 1 else "s",
                 result.dirs, "y" if result.dirs == 1 else "ies"))
        return 0
    for problem in result.problems:
        print("FAIL: %s" % problem, file=sys.stderr)
    return 1


def cmd_boards(args: argparse.Namespace) -> int:
    boards = boards_mod.load_boards()
    if args.name:
        try:
            b = boards_mod.get_board(args.name)
        except KeyError as e:
            print("error: %s" % e, file=sys.stderr)
            return 2
        print("%s - %s" % (b.name, b.display_name))
        if b.unsupported:
            print("  RETIRED:  %s" % b.unsupported)
        print("  arch:     %s" % b.arch)
        if b.mpy_args:
            print("  mpy_args: %s   (used by `build romfs` when compiling .py)" % " ".join(b.mpy_args))
        for p in b.partitions:
            rules = ", ".join("%s=%d" % (r["extension"], r["alignment"])
                              for r in p.alignment_rules) or "(none)"
            print("  partition [%d] %-22s %-13s size %-9s align %s"
                  % (p.index, p.name, "(%s)" % p.role, _human(p.size), rules))
            if p.npu:
                print("                 npu: %s   (used by `build romfs` to convert models)"
                      % p.npu.get("type"))
        return 0

    width = max(len(n) for n in boards)
    for name in sorted(boards):
        b = boards[name]
        sizes = "/".join(_human(p.size) for p in b.partitions)
        tail = "  (retired -- no longer supported)" if b.unsupported else ""
        print("%-*s  %-34s  %s%s" % (width, name, b.display_name, sizes, tail))
    return 0

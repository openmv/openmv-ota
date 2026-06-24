#!/usr/bin/env python3
"""Capability-aware build driver: prove the openmv-ota tools work -- or fail
*cleanly* -- for every board, on whatever host runs this.

For each board it derives the expected capability from the bundled board data and
the OTA geometry rules (the same source the tool itself uses), runs the real
build / inspect / verify commands, and asserts the outcome:

  full     (OTA-capable):   project new --ota; build firmware + romfs +
                            factory-romfs; inspect + verify the OTA bundle;
                            a corrupted body must FAIL verify; verify both
                            factory slots (FRONT confirmed-shape, BACK golden).
  classic  (romfs, not OTA): project new; build firmware + romfs (single image);
                            `project new --ota` must fail cleanly (not OTA-capable);
                            `build factory-romfs` must fail cleanly (needs OTA).
  noromfs  (no partition):   `project new` must fail cleanly (no partition size).

Every *expected* failure is also asserted to be a clean, single-line tool error
(no Python traceback, no wall of `make` output) -- that is the whole point: a
board the tool can't serve says so structurally, it doesn't explode.

Exit 0 iff every expectation held for every selected board.

Usage:
    python ci/build_boards.py --firmware /path/to/openmv [--boards OPENMV_N6 ...]
                              [--workdir DIR] [--no-firmware] [--install-sdk]
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from openmv_ota.ota import bundle, geometry, parse_trailer, read_trusted_keys
from openmv_ota.ota.verify import verify_image
from openmv_ota.romfs import boards as boards_mod

OTA = os.environ.get("OPENMV_OTA_BIN", "openmv-ota")


class Report:
    """Collects PASS/FAIL lines; non-zero exit iff anything failed."""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.total = 0

    def check(self, cond: bool, label: str, detail: str = "") -> bool:
        self.total += 1
        mark = "PASS" if cond else "FAIL"
        line = "  [%s] %s" % (mark, label)
        if not cond and detail:
            line += "\n         %s" % detail.replace("\n", "\n         ")
        print(line, flush=True)
        if not cond:
            self.failures.append(label)
        return cond


def run(args: list[str], cwd: Path | None = None) -> tuple[int, str]:
    """Run an openmv-ota subcommand, capturing combined output."""
    p = subprocess.run([OTA, *args], cwd=str(cwd) if cwd else None,
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def is_clean_failure(rc: int, out: str, *, code: int | None = None,
                     needle: str | None = None) -> tuple[bool, str]:
    """A *structural* failure: non-zero exit, no Python traceback, an ``error:``
    line, and (optionally) a specific exit code / message fragment."""
    if "Traceback (most recent call last)" in out:
        return False, "produced a Python traceback:\n" + out
    if rc == 0:
        return False, "expected failure but exit was 0"
    if code is not None and rc != code:
        return False, "expected exit %d, got %d:\n%s" % (code, rc, out)
    if "error:" not in out:
        return False, "no 'error:' line in output:\n" + out
    if needle is not None and needle not in out:
        return False, "expected message %r, got:\n%s" % (needle, out)
    return True, ""


def classify(board: str) -> tuple[str, int, int]:
    """('full'|'classic'|'noromfs', partition_size, erase_size) from board data."""
    part = boards_mod.get_board(board).partition()  # partition 0
    size, erase = part.size, part.erase_size
    if size <= 0:
        return "noromfs", size, erase
    if geometry.is_ota_capable(size, erase):
        return "full", size, erase
    return "classic", size, erase


def _project_new(proj: Path, board: str, firmware: Path, *, ota: bool,
                 install_sdk: bool) -> tuple[int, str]:
    args = ["project", "new", str(proj), "-f", str(firmware), "-b", board]
    if ota:
        args.append("--ota")
    if install_sdk:
        args.append("--install-sdk")
    return run(args)


def _verify_factory(rep: Report, proj: Path, board: str, img: Path) -> None:
    """Decode the dual-slot factory image and verify each slot's trailer + status,
    using the project's resolved geometry (the authoritative source)."""
    from openmv_ota.project import load_project
    from openmv_ota.ota import status as status_mod

    p = load_project(proj)
    t = p.board(board)
    block = geometry.ota_block(t.erase_size)
    data = img.read_bytes()
    rep.check(len(data) == t.partition_size, "factory image is the full partition",
              "got %d, want %d" % (len(data), t.partition_size))
    trusted = read_trusted_keys(proj / "keys" / "trusted_keys.json")
    front, back = data[:t.front_size], data[t.front_size:]

    def slot_ok(name: str, slot: bytes, want_pending: bool) -> None:
        tb = slot[-block:]                       # trailer in the slot's last block
        tr = parse_trailer(tb)
        ok, reason = verify_image(slot[:tr.body_size], tb, trusted)
        rep.check(ok, "factory %s slot verifies (factory key)" % name, reason)
        ss = slot[-2 * block:-block]             # status sector precedes the trailer
        marks = (ss[0:16] == status_mod.PENDING, ss[16:32] == status_mod.TRIED,
                 ss[32:48] == status_mod.CONFIRMED)
        want = (want_pending, want_pending, True)  # FRONT: all set; BACK: confirmed only
        rep.check(marks == want, "factory %s slot status markers" % name,
                  "got %s, want %s" % (marks, want))

    slot_ok("FRONT", front, want_pending=True)
    slot_ok("BACK", back, want_pending=False)


def _verify_bundle(rep: Report, proj: Path, zip_path: Path) -> None:
    """inspect + verify the OTA bundle, and prove a corrupted body FAILS verify."""
    rc, out = run(["build", "inspect", str(zip_path)])
    rep.check(rc == 0, "build inspect decodes the bundle", out)
    rc, out = run(["build", "verify", str(zip_path)], cwd=proj)
    rep.check(rc == 0, "build verify passes (signature + body hash)", out)

    body, trailer = bundle.read_bundle(zip_path)
    tampered = bytearray(body)
    tampered[0] ^= 0xFF                           # flip one body byte
    with tempfile.TemporaryDirectory() as td:
        b = Path(td) / "romfs.img"
        t = Path(td) / "trailer.bin"
        b.write_bytes(bytes(tampered))
        t.write_bytes(trailer)
        rc, out = run(["build", "verify", str(b), str(t)], cwd=proj)
    # A verification *verdict* (exit 1, "FAILED"), not a structural tool error.
    rejected = (rc == 1 and "FAILED" in out
                and "Traceback (most recent call last)" not in out)
    rep.check(rejected, "build verify REJECTS a corrupted body (exit 1)", out)


def do_full(rep: Report, board: str, firmware: Path, work: Path, *,
            do_firmware: bool, install_sdk: bool) -> None:
    proj = work / "ota"
    rc, out = _project_new(proj, board, firmware, ota=True, install_sdk=install_sdk)
    if not rep.check(rc == 0, "project new --ota succeeds", out):
        return

    if do_firmware:
        rc, out = run(["build", "firmware", str(proj), "-b", board])
        rep.check(rc == 0, "build firmware succeeds", out)
        fw = list((proj / "build").glob("%s-firmware*.bin" % board))
        rep.check(bool(fw), "firmware image written (<board>-firmware*.bin)",
                  "found: %s" % [f.name for f in fw])

    rc, out = run(["build", "romfs", str(proj), "-b", board])
    rep.check(rc == 0, "build romfs (OTA) succeeds", out)
    zip_path = proj / "build" / ("%s-romfs.zip" % board)
    if rep.check(zip_path.exists(), "OTA bundle written (<board>-romfs.zip)"):
        _verify_bundle(rep, proj, zip_path)

    rc, out = run(["build", "factory-romfs", str(proj), "-b", board])
    rep.check(rc == 0, "build factory-romfs succeeds", out)
    img = proj / "build" / ("%s-factory-romfs.img" % board)
    if rep.check(img.exists(), "factory image written (<board>-factory-romfs.img)"):
        _verify_factory(rep, proj, board, img)


def do_classic(rep: Report, board: str, firmware: Path, work: Path, *,
               do_firmware: bool, install_sdk: bool) -> None:
    # --ota must be refused, structurally, before anything is written.
    rc, out = _project_new(work / "ota_attempt", board, firmware, ota=True,
                          install_sdk=install_sdk)
    ok, detail = is_clean_failure(rc, out, code=1, needle="not OTA-capable")
    rep.check(ok, "project new --ota refused cleanly (not OTA-capable)", detail)

    proj = work / "plain"
    rc, out = _project_new(proj, board, firmware, ota=False, install_sdk=install_sdk)
    if not rep.check(rc == 0, "project new (non-OTA) succeeds", out):
        return

    if do_firmware:
        rc, out = run(["build", "firmware", str(proj), "-b", board])
        rep.check(rc == 0, "build firmware succeeds", out)
        fw = list((proj / "build").glob("%s-firmware*.bin" % board))
        rep.check(bool(fw), "firmware image written (<board>-firmware*.bin)",
                  "found: %s" % [f.name for f in fw])

    rc, out = run(["build", "romfs", str(proj), "-b", board])
    rep.check(rc == 0, "build romfs (single image) succeeds", out)
    rep.check((proj / "build" / ("%s-romfs.img" % board)).exists(),
              "single image written (<board>-romfs.img)")

    rc, out = run(["build", "factory-romfs", str(proj), "-b", board])
    ok, detail = is_clean_failure(rc, out, code=1, needle="OTA project")
    rep.check(ok, "build factory-romfs refused cleanly (needs OTA project)", detail)


def do_noromfs(rep: Report, board: str, firmware: Path, work: Path, *,
               install_sdk: bool, **_: object) -> None:
    rc, out = _project_new(work / "plain", board, firmware, ota=False,
                          install_sdk=install_sdk)
    ok, detail = is_clean_failure(rc, out)
    rep.check(ok, "project new refused cleanly (no ROMFS partition)", detail)


HANDLERS = {"full": do_full, "classic": do_classic, "noromfs": do_noromfs}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--firmware", required=True, help="local OpenMV firmware checkout")
    ap.add_argument("--boards", nargs="*", help="boards to test (default: all known)")
    ap.add_argument("--workdir", help="where to create projects (default: a temp dir)")
    ap.add_argument("--no-firmware", action="store_true",
                    help="skip the slow firmware compile (still builds romfs/factory)")
    ap.add_argument("--install-sdk", action="store_true",
                    help="pass --install-sdk to project new (download the SDK if missing)")
    args = ap.parse_args(argv)

    firmware = Path(args.firmware).resolve()
    boards = args.boards or boards_mod.board_names()
    base = Path(args.workdir).resolve() if args.workdir else Path(tempfile.mkdtemp(
        prefix="openmv-ota-ci-"))
    base.mkdir(parents=True, exist_ok=True)

    rep = Report()
    for board in boards:
        kind, size, erase = classify(board)
        print("\n=== %s  (%s; %d-byte partition, %d-byte erase) ==="
              % (board, kind, size, erase), flush=True)
        work = base / board
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        HANDLERS[kind](rep, board, firmware, work,
                       do_firmware=not args.no_firmware, install_sdk=args.install_sdk)

    print("\n" + "=" * 60)
    if rep.failures:
        print("FAILED %d/%d checks:" % (len(rep.failures), rep.total))
        for f in rep.failures:
            print("  - %s" % f)
        return 1
    print("OK: all %d checks passed across %d board(s)" % (rep.total, len(boards)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

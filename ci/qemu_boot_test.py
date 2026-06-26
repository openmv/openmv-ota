#!/usr/bin/env python3
"""QEMU integration test for the device OTA ``boot.py``.

Runs the **real** frozen ``boot.py`` source on actual MicroPython, under
``qemu-system-arm`` (the MPS2-AN500 Cortex-M7 and MPS3-AN547 Cortex-M55 boards),
and checks the parts the host unit tests can't reach: that boot.py behaves the
same on MicroPython (on both architectures), and that the real ``vfs.rom_ioctl``
read + ``vfs.VfsRom`` mount + slot selection work on-device.

Two kinds of check, both driven over the QEMU serial REPL via the firmware's
bundled ``mpremote`` (``run`` a pasted script -- no filesystem mount needed):

1. **All boot paths** -- ``evaluate_slot``/``parse_trailer`` are exercised with
   crafted trailers + an injected ``verify`` for every reject reason and the valid
   cases, mirroring the host suite but on MicroPython. (The ECDSA C shim itself is
   covered by the 100%-gcov host test; mbedtls isn't built for the qemu port yet.)
2. **Real mount** -- a partitioned romfs (FRONT + BACK) is loaded into the
   emulated XIP region; ``OtaBoot.run`` reads it through ``vfs.rom_ioctl`` and
   mounts the chosen slot. Valid -> FRONT; a corrupted FRONT body -> BACK.

Usage:
    qemu_boot_test.py --firmware /path/to/openmv [--board MPS2_AN500 ...]

Each requested board needs ``build/<board>/bin/firmware.elf`` in the checkout
(``make TARGET=<board>``); boards without one are skipped. Needs
``qemu-system-arm``. Exit 0 iff all run checks pass.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from openmv_ota.ota import status as host_status
from openmv_ota.ota import trailer as host_trailer
from openmv_ota.ota.algorithms import ES256, algorithm_for
from openmv_ota.romfs.builder import build_image

BOOT_PY = (Path(__file__).resolve().parent.parent / "src" / "openmv_ota"
           / "build" / "device" / "boot.py")
BLOCK = 4096
BOARD = 0x1234            # the trailer board_id the path cases use (a test value)
PLAT = 5 << 24
V1 = 1 << 24

# Per-board: qemu machine, romfs XIP origin, and partition + FRONT slot sizes.
BOARDS = {
    "MPS2_AN500": dict(machine="mps2-an500", origin=0x60C00000, part=4194304, front=2097152),
    "MPS3_AN547": dict(machine="mps3-an547", origin=0x62000000, part=33554432, front=16777216),
}


# --- host-side fixture builders --------------------------------------------

def _trailer(body, *, board=BOARD, minplat=0, pv=V1, floor=0, body_size=None, key_id=0x100):
    spec = algorithm_for(ES256)
    t = host_trailer.Trailer(
        body_size=len(body) if body_size is None else body_size, pad_size=0,
        meta={"k": 1}, board_id=board, min_platform_version=minplat, payload_version=pv,
        payload_version_floor=floor, key_id=key_id, sig_alg=ES256,
        body_sha256=hashlib.sha256(body).digest())
    t.signature = b"\x11" * spec.sig_size       # arbitrary; verify is injected on-device
    return host_trailer.pack_trailer(t)


def _status(p, tr, c):
    return host_status.build_status_sector(BLOCK, pending=p, tried=tr, confirmed=c)


def _path_cases():
    """(label, body, status, trailer, is_front, floor, board, plat, verify_ret, expected).
    Board-independent: exercises evaluate_slot/parse_trailer with in-memory bytes."""
    b = b"app" * 40
    tb = _trailer(b)
    bad_crc = tb[:-1] + bytes([tb[-1] ^ 0xFF])
    corrupt = bytearray(b)
    corrupt[0] ^= 0xFF
    C = [
        ("front_confirmed", b, _status(1, 1, 1), tb, True, 0, BOARD, PLAT, True, "OK"),
        ("front_trial_arm", b, _status(1, 0, 0), tb, True, 0, BOARD, PLAT, True, "OK"),
        ("sig_reject", b, _status(1, 1, 1), tb, True, 0, BOARD, PLAT, False, "sig"),
        ("key_unknown", b, _status(1, 1, 1), tb, True, 0, BOARD, PLAT, True, "key"),
        ("bad_magic", b, _status(1, 1, 1), b"XX" + tb[2:], True, 0, BOARD, PLAT, True, "magic"),
        ("bad_crc", b, _status(1, 1, 1), bad_crc, True, 0, BOARD, PLAT, True, "crc"),
        ("board_mismatch", b, _status(1, 1, 1), _trailer(b, board=0x9999), True, 0, BOARD, PLAT, True, "board"),
        ("compat_old", b, _status(1, 1, 1), _trailer(b, minplat=6 << 24), True, 0, BOARD, PLAT, True, "compat"),
        ("body_sha", bytes(corrupt), _status(1, 1, 1), tb, True, 0, BOARD, PLAT, True, "body-sha"),
        ("rollback", b, _status(1, 1, 1), tb, True, 2 << 24, BOARD, PLAT, True, "rollback"),
        ("trial_failed", b, _status(1, 1, 0), tb, True, 0, BOARD, PLAT, True, "trial-failed"),
        ("forged_confirm", b, _status(0, 0, 1), tb, True, 0, BOARD, PLAT, True, "forged-confirm"),
        ("status_none", b, _status(0, 0, 0), tb, True, 0, BOARD, PLAT, True, "status"),
        ("back_factory", b, _status(0, 0, 1), tb, False, 0, BOARD, PLAT, True, "OK"),
        ("back_not_factory", b, _status(1, 1, 1), tb, False, 0, BOARD, PLAT, True, "back-not-factory"),
    ]
    return [(lbl, body.hex(), st.hex(), tr.hex(), isf, fl, bd, pl, vr, exp)
            for (lbl, body, st, tr, isf, fl, bd, pl, vr, exp) in C]


def _vfsrom(marker: str) -> bytes:
    """A mountable VfsRom body carrying a one-file marker + a system.json."""
    src = Path(tempfile.mkdtemp(prefix="omv-qemu-rom-"))
    (src / "slot_marker.txt").write_text(marker)
    (src / "system.json").write_text('{"board":"qemu","ota":true}\n')
    img = build_image(str(src))
    shutil.rmtree(src, ignore_errors=True)
    return img


def _slot(body: bytes, status: bytes, trailer: bytes, slot_size: int) -> bytes:
    out = bytearray(b"\xff" * slot_size)
    out[0:len(body)] = body
    out[slot_size - 2 * BLOCK:slot_size - 2 * BLOCK + len(status)] = status
    out[slot_size - BLOCK:slot_size - BLOCK + len(trailer)] = trailer
    return bytes(out)


def _partition(part: int, front: int, corrupt_front=False, front_status=None) -> bytes:
    """A FRONT + BACK (golden) romfs partition with mountable bodies. FRONT defaults to
    the confirmed shape; pass ``front_status`` (e.g. pending-only) to exercise arming."""
    fb, bb = _vfsrom("SLOT=FRONT"), _vfsrom("SLOT=BACK")
    fs = front_status if front_status is not None else _status(1, 1, 1)
    img = bytearray(_slot(fb, fs, _trailer(fb, board=0), front)
                    + _slot(bb, _status(0, 0, 1), _trailer(bb, board=0), part - front))
    if corrupt_front:
        img[0] ^= 0xFF                       # break FRONT body SHA -> fall back to BACK
    return bytes(img)


# --- device-side runner scripts (pasted into the REPL via mpremote run) ------

def _paths_script() -> str:
    runner = '''
import binascii
def _h(s): return binascii.unhexlify(s)
_CASES = %r
_PUB = _h("04" + "00" * 64)
_fail = 0; _n = 0
for (label, bh, sh, th, isf, fl, bd, pl, vr, exp) in _CASES:
    _n += 1
    trusted = {} if label == "key_unknown" else {0x100: _PUB}
    try:
        evaluate_slot(_h(bh), _h(sh), _h(th), isf, fl, bd, trusted, pl,
                      (lambda a, p, s, m, _v=vr: _v))
        got = "OK"
    except OtaReject as e:
        got = str(e)
    if got != exp:
        _fail += 1
    print(("PASS " if got == exp else "FAIL ") + label + " got=" + got + " want=" + exp)
print("RESULT", "PASS" if _fail == 0 else "FAIL", str(_n - _fail) + "/" + str(_n))
''' % (_path_cases(),)
    return BOOT_PY.read_text() + "\n" + runner


def _mount_script(part: int, front: int) -> str:
    runner = '''
import os, vfs, binascii, uctypes
_base = uctypes.addressof(vfs.rom_ioctl(2, 0))   # mirror boot.py's _main read seam
def _read(off, size): return uctypes.bytearray_at(_base + off, size)
def _mnt(body):
    try: vfs.umount("/rom")
    except Exception: pass
    vfs.mount(vfs.VfsRom(body), "/rom")
_T = {0x100: binascii.unhexlify("04" + "00" * 64)}
slot, tr, reason = OtaBoot(_read, (lambda a, p, s, m: True), _mnt, (lambda o, m: None),
                          %d, %d, %d, 0, _T, 0x7fffffff).run()
mk = open("/rom/slot_marker.txt").read().strip()
print("SLOT", slot, "REASON", reason, "MARKER", mk)
''' % (part, front, BLOCK)
    return BOOT_PY.read_text() + "\n" + runner


def _arm_fail_script(part: int, front: int) -> str:
    # Like _mount_script but with the *real* verified write_marker (rom_ioctl + read
    # back). On the read-only qemu port arming 'tried' fails, so Option B must fall
    # back to the golden BACK image with reason 'trial-arm'.
    runner = '''
import vfs, binascii, uctypes
_base = uctypes.addressof(vfs.rom_ioctl(2, 0))
def _read(off, size): return uctypes.bytearray_at(_base + off, size)
def _mnt(body):
    try: vfs.umount("/rom")
    except Exception: pass
    vfs.mount(vfs.VfsRom(body), "/rom")
def _wm(off, marker):
    if vfs.rom_ioctl(4, 0, off, marker) < 0: raise OSError("write failed")
    if _read(off, len(marker)) != marker: raise OSError("verify failed")
_T = {0x100: binascii.unhexlify("04" + "00" * 64)}
slot, tr, reason = OtaBoot(_read, (lambda a, p, s, m: True), _mnt, _wm,
                          %d, %d, %d, 0, _T, 0x7fffffff).run()
mk = open("/rom/slot_marker.txt").read().strip()
print("SLOT", slot, "REASON", reason, "MARKER", mk)
''' % (part, front, BLOCK)
    return BOOT_PY.read_text() + "\n" + runner


# --- openmv_ota runtime library (status/confirm/sync) -----------------------

_RUNTIME_LIB = (Path(__file__).resolve().parent.parent / "src" / "openmv_ota"
                / "build" / "device" / "openmv_ota" / "__init__.py")


def _runtime_partition(part: int, front: int) -> bytes:
    """A partition whose FRONT body is a romfs carrying the real openmv_ota runtime
    lib + a matching _ota_config + a sync() resource, with the FRONT status sector
    crafted as an un-confirmed one-shot trial (pending + tried)."""
    src = Path(tempfile.mkdtemp(prefix="omv-qemu-rt-"))
    (src / "lib" / "openmv_ota" / "data").mkdir(parents=True)
    (src / "lib" / "openmv_ota" / "__init__.py").write_text(_RUNTIME_LIB.read_text())
    (src / "lib" / "openmv_ota" / "data" / "resources.json").write_text(
        '[{"file":"coprocessor.romfs","handler":"partition","partition":0,"name":"probe"}]')
    (src / "lib" / "openmv_ota" / "data" / "coprocessor.romfs").write_bytes(b"COPRO-IMAGE")
    (src / "system.json").write_text('{"board": "qemu", "app_version": "1.2.3"}')
    (src / "_ota_config.py").write_text(
        "PARTITION_SIZE=%d\nFRONT_SIZE=%d\nOTA_BLOCK=%d\n"
        "BOARD_ID=0\nPLATFORM_VERSION=0\nTRUSTED_KEYS={}\n" % (part, front, BLOCK))
    body = build_image(str(src))
    shutil.rmtree(src, ignore_errors=True)
    img = bytearray(b"\xff" * part)
    img[0:len(body)] = body
    so = front - 2 * BLOCK                         # FRONT status sector offset
    img[so:so + 16] = host_status.PENDING          # craft an un-confirmed trial
    img[so + 16:so + 32] = host_status.TRIED
    return bytes(img)


def _runtime_script() -> str:
    # mp_init auto-mounts the partition's romfs at /rom and adds /rom/lib to sys.path,
    # so the lib + _ota_config import directly. We set _ota_config.last_slot ourselves
    # (the channel boot.py's _main mirrors its result onto) since this script doesn't run
    # the real boot. Then: status() reflects the slot + the crafted trial; identity()
    # reads /rom/system.json; confirm() on a FRONT trial reaches its flash write, which
    # the read-only qemu port rejects -> raises (verifying read/decide/plan + the rc
    # check); the slot guard makes confirm() a no-op once we pretend we fell back to BACK;
    # sync() likewise reaches its write and raises. (Real-hardware writes succeed; that
    # path is host-logic-tested.)
    return '''
import openmv_ota as o
import _ota_config
_ota_config.last_slot = "FRONT"
_ota_config.last_failure_reason = None
s = o.status()
ident = o.identity()
try:
    o.confirm(); cw = "no-raise"
except OSError:
    cw = "raised"
_ota_config.last_slot = "BACK"          # pretend we fell back -> confirm() must no-op
guard = (o.confirm() is False)
try:
    o.sync(); sw = "no-raise"
except OSError:
    sw = "raised"
ok = (s["trial"] and s["slot"] == "FRONT" and ident.get("board") == "qemu"
      and cw == "raised" and guard and sw == "raised")
print("RT trial=%s slot=%s id=%s confirm=%s guard=%s sync=%s"
      % (s["trial"], s["slot"], ident.get("board"), cw, guard, sw))
print("RTRESULT", "PASS" if ok else "FAIL")
'''


# --- openmv_ota installer (exec-into-RAM + DeflateIO + write loop) -----------

_INSTALLER = (Path(__file__).resolve().parent.parent / "src" / "openmv_ota"
              / "build" / "device" / "openmv_ota" / "data" / "installer.py")
_LOG = (Path(__file__).resolve().parent.parent / "src" / "openmv_ota"
        / "build" / "device" / "openmv_log.py")


def _installer_partition(fw: Path, part: int, front: int) -> bytes:
    """A partition whose FRONT romfs carries the runtime lib + the installer *source*
    (data/installer.py) + a fake ca.pem + a matching _ota_config -- enough to exec the
    installer into RAM and exercise its logic on real MicroPython. Also ships openmv_log +
    the real micropython-lib logging.py (the emulator boards don't freeze logging, but
    real OpenMV boards do), so the logging-based logger can be exercised too."""
    src = Path(tempfile.mkdtemp(prefix="omv-qemu-inst-"))
    data = src / "lib" / "openmv_ota" / "data"
    data.mkdir(parents=True)
    (src / "lib" / "openmv_ota" / "__init__.py").write_text(_RUNTIME_LIB.read_text())
    (data / "installer.py").write_text(_INSTALLER.read_text())
    (data / "ca.pem").write_text("-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n")
    (src / "openmv_log.py").write_text(_LOG.read_text())   # importable from /rom for this test
    logging_lib = (fw / "lib" / "micropython" / "lib" / "micropython-lib"
                   / "python-stdlib" / "logging" / "logging.py")
    (src / "logging.py").write_text(logging_lib.read_text())
    (src / "_ota_config.py").write_text(
        "PARTITION_SIZE=%d\nFRONT_SIZE=%d\nOTA_BLOCK=%d\n"
        "BOARD_ID=0\nPLATFORM_VERSION=0\nTRUSTED_KEYS={}\n" % (part, front, BLOCK))
    body = build_image(str(src))
    shutil.rmtree(src, ignore_errors=True)
    img = bytearray(b"\xff" * part)
    img[0:len(body)] = body
    return bytes(img)


# Exec the installer source (as install() does on-device) and exercise the parts host
# tests can't reach on MicroPython: the io.IOBase + deflate.DeflateIO decompress chain,
# the chunked/content-length body de-framing, and the erase/write/arm loop over a fake
# flash. __GZ__/__PAYLOAD__ are substituted with bytes literals (avoids %-escaping).
_INSTALLER_SCRIPT_TMPL = '''
import sys


class _FakeTime:                                # the qemu port has no RTC (no time.time);
    @staticmethod                               # fake it so the logging emit path can run
    def time():
        return 1782000000

    @staticmethod
    def localtime(t=None):
        return (2026, 6, 25, 12, 34, 56, 2, 176)

    @staticmethod
    def ticks_ms():
        return 12345


sys.modules["time"] = _FakeTime                 # before openmv_log/logging import time

ns = {}
exec(open("/rom/lib/openmv_ota/data/installer.py").read(), ns)
P = ns


def recv_of(*pieces):
    box = list(pieces)

    def recv(n):
        return box.pop(0) if box else b""
    return recv


host, port, path = P["_parse_url"]("https://h.io:8443/o.img.gz?x=1")
url_ok = host == "h.io" and port == 8443 and path == "/o.img.gz?x=1"
blank_ok = P["_is_blank"](b"\\xff\\xff") and not P["_is_blank"](b"\\xff\\x00")
chunk_ok = P["_chunk_size"](b"1a;ext\\r\\n") == 0x1a

b = P["_make_body"](P["_Reader"](recv_of(b"HELLOworld")), {b"content-length": b"5"})
buf = bytearray(8)
n = b.readinto(buf)
body_ok = bytes(buf[:n]) == b"HELLO"

import deflate
GZ = __GZ__
b2 = P["_make_body"](P["_Reader"](recv_of(GZ)), {b"content-length": str(len(GZ)).encode()})
dio = deflate.DeflateIO(b2, deflate.GZIP)
out = b""
while True:
    c = dio.read(64)
    if not c:
        break
    out += c
deflate_ok = out == __PAYLOAD__

BLOCK = 4096
FRONT = 3 * BLOCK
mem = bytearray(b"\\x00" * FRONT)


def erase(total):
    mem[:total] = b"\\xff" * total


def write(off, d):
    mem[off:off + len(d)] = d


def readback(off, m):
    return bytes(mem[off:off + m])


def reader_of(d):
    box = [d]

    def read(m):
        r = box[0][:m]
        box[0] = box[0][m:]
        return r
    return read


img = bytearray(b"\\xff" * FRONT)
img[0:4] = b"DATA"
fed = []
prog = []
P["_install_stream"](reader_of(bytes(img)), erase, write, readback, FRONT, BLOCK,
                     lambda: fed.append(1), lambda d, t: prog.append((d, t)))
so = FRONT - 2 * BLOCK
install_ok = (mem[0:4] == b"DATA" and bytes(mem[so:so + 16]) == P["PENDING"]
              and len(fed) > 0           # fed the watchdog per chunk
              and prog and prog[-1] == (FRONT, FRONT))   # progress reported to 100%

import openmv_log                                 # imports logging + configures the logger
import logging
import io
fmt_ok = (openmv_log._format("12.345", "INFO", "openmv_ota", "x")
          == "[12.345] INFO openmv_ota: x"
          and openmv_log._stamp((2026, 6, 25, 12, 34, 56, 0, 0), 0) == "2026-06-25 12:34:56"
          and openmv_log._stamp((2000, 1, 1, 0, 0, 0, 0, 0), 12345) == "   12.345")
# Full emit path through the real micropython-lib logging.py + our _OtaFormatter (with
# the RTC set via _FakeTime -> wall-clock stamp), captured to a buffer.
buf = io.StringIO()
_h = logging.StreamHandler(buf)
_h.terminator = "\\r\\n"
_h.setFormatter(openmv_log._OtaFormatter())
openmv_log.log.addHandler(_h)
openmv_log.log.setLevel(logging.INFO)
openmv_log.log.warning("qemu: live-log")
emit_ok = buf.getvalue() == "[2026-06-25 12:34:56] WARNING openmv_ota: qemu: live-log\\r\\n"

ok = (url_ok and blank_ok and chunk_ok and body_ok and deflate_ok and install_ok
      and fmt_ok and emit_ok)
print("INST", "url=" + str(url_ok), "deflate=" + str(deflate_ok),
      "install=" + str(install_ok), "log=" + str(fmt_ok), "emit=" + str(emit_ok))
print("INSTRESULT", "PASS" if ok else "FAIL")
'''


def _installer_script() -> str:
    import gzip
    payload = b"openmv-ota installer payload " * 40
    gz = gzip.compress(payload, mtime=0)
    return (_INSTALLER_SCRIPT_TMPL
            .replace("__GZ__", repr(gz)).replace("__PAYLOAD__", repr(payload)))


# --- qemu orchestration -----------------------------------------------------

def _run_scenario(fw: Path, mpremote: Path, board: str, romfs: bytes, script: str,
                  timeout=120) -> str:
    """Boot ``board`` under qemu with ``romfs`` loaded, paste ``script`` over the
    serial REPL via mpremote, and return its stdout."""
    geom = BOARDS[board]
    elf = fw / "build" / board / "bin" / "firmware.elf"
    tmp = Path(tempfile.mkdtemp(prefix="omv-qemu-"))
    (tmp / "romfs0.img").write_bytes(romfs)
    (tmp / "run.py").write_text(script)
    qserial = tmp / "qserial.txt"
    qemu = subprocess.Popen(
        ["qemu-system-arm", "-machine", geom["machine"], "-display", "none",
         "-monitor", "null", "-semihosting",
         "-device", "loader,file=%s,addr=0x%X,force-raw=on" % (tmp / "romfs0.img", geom["origin"]),
         "-serial", "pty", "-kernel", str(elf)],
        stdin=subprocess.DEVNULL, stdout=open(qserial, "wb"), stderr=subprocess.STDOUT)
    try:
        pts = _await_pts(qserial, qemu, deadline=time.monotonic() + 40)
        out = subprocess.run(
            [sys.executable, str(mpremote), "connect", pts, "run", str(tmp / "run.py")],
            capture_output=True, text=True, timeout=timeout)
        return out.stdout + out.stderr
    finally:
        qemu.terminate()
        try:
            qemu.wait(timeout=5)
        except subprocess.TimeoutExpired:
            qemu.kill()
        shutil.rmtree(tmp, ignore_errors=True)


def _await_pts(qserial: Path, qemu, deadline) -> str:
    while time.monotonic() < deadline:
        if qemu.poll() is not None:
            raise RuntimeError("qemu exited early: " + qserial.read_text(errors="replace"))
        m = re.search(r"/dev/pts/\d+", qserial.read_text(errors="replace") or "")
        if m:
            return m.group(0)
        time.sleep(0.25)
    raise RuntimeError("qemu never reported a serial pty")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--firmware", required=True, help="openmv checkout with the board(s) built")
    ap.add_argument("--board", choices=list(BOARDS), action="append",
                    help="board(s) to test (default: every board with a built firmware)")
    args = ap.parse_args(argv)
    fw = Path(args.firmware).resolve()
    mpremote = fw / "lib" / "micropython" / "tools" / "mpremote" / "mpremote.py"
    if not shutil.which("qemu-system-arm"):
        print("error: qemu-system-arm not found", file=sys.stderr)
        return 2

    ok = True
    ran = 0

    def section(title, out, predicate):
        nonlocal ok
        good = predicate(out)
        ok = ok and good
        print("\n=== %s : %s ===" % (title, "PASS" if good else "FAIL"))
        print(out.strip())

    for board in (args.board or list(BOARDS)):
        if not (fw / "build" / board / "bin" / "firmware.elf").exists():
            print("\n--- %s: no firmware.elf, skipping (build with make TARGET=%s) ---"
                  % (board, board))
            continue
        ran += 1
        geom = BOARDS[board]
        part, front = geom["part"], geom["front"]
        section("%s boot paths" % board,
                _run_scenario(fw, mpremote, board, _partition(part, front), _paths_script()),
                lambda o: "RESULT PASS" in o)
        section("%s real mount -> FRONT" % board,
                _run_scenario(fw, mpremote, board, _partition(part, front), _mount_script(part, front)),
                lambda o: "SLOT FRONT REASON None MARKER SLOT=FRONT" in o)
        section("%s corrupt FRONT -> BACK" % board,
                _run_scenario(fw, mpremote, board, _partition(part, front, corrupt_front=True),
                              _mount_script(part, front)),
                lambda o: "SLOT BACK REASON body-sha" in o)
        section("%s arm 'tried' fails -> BACK" % board,
                _run_scenario(fw, mpremote, board,
                              _partition(part, front, front_status=_status(1, 0, 0)),
                              _arm_fail_script(part, front)),
                lambda o: "SLOT BACK REASON trial-arm" in o)
        section("%s openmv_ota runtime (status/confirm/sync)" % board,
                _run_scenario(fw, mpremote, board, _runtime_partition(part, front),
                              _runtime_script()),
                lambda o: "RTRESULT PASS" in o)
        section("%s openmv_ota installer (exec + DeflateIO + write loop)" % board,
                _run_scenario(fw, mpremote, board, _installer_partition(fw, part, front),
                              _installer_script()),
                lambda o: "INSTRESULT PASS" in o)

    print("\n" + "=" * 50)
    if ran == 0:
        print("QEMU boot test: no firmware built for any requested board", file=sys.stderr)
        return 2
    print("QEMU boot test:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

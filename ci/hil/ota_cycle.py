#!/usr/bin/env python3
"""HIL OTA-cycle test + coverage trace.

Provision a golden board from the CURRENT tree, publish an update, and verify the
device installs it, trials it, confirms it, and promotes it -- fully autonomously --
while capturing the HILCOV markers off the board's P4/P5 side-channel UART. Emit a
PASS/FAIL plus a JSON trace (versions, timings, and the set of coverage markers this
run actually executed on the live device).

Runs ON the board's self-hosted runner (USB access to the board + its UART bridge).
Board-specific flash + network live in ``BOARDS`` below; bench-wide config comes from
the environment so nothing secret is committed:

    OTA_SERVER      update server base URL           (default https://192.168.0.100:8443)
    OTA_TOKEN       admin token for publish + query  (default bench-admin-token-1)
    OTA_CA_NODE     CA path ON THE NODE              (default ~/bench-ca.pem)
    OTA_CA_BOARD    CA path ON THE BOARD             (default /flash/bench-ca.pem)
    WIFI_SSID/WIFI_PASSWORD   for WiFi boards
    PROJECT_DIR     the pegged project on the node   (default ~/proj)
    OTA_VENV, SDK_HOME, JLINK, DFU_UTIL, MPREMOTE    tool paths (sensible defaults)
    BOARD_ACM       board USB-CDC serial             (default /dev/ttyACM0)
    BOARD_UART      the P4/P5 UART bridge on the node (default /dev/ttyUSB0)

This is a live-hardware gate, not a host unit test -- it is invoked by the
``hil-ota`` workflow (workflow_dispatch), never per-commit.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

HOME = os.path.expanduser("~")


def env(name, default):
    return os.environ.get(name, default)


CFG = {
    "server": env("OTA_SERVER", "https://192.168.0.100:8443"),
    "token": env("OTA_TOKEN", "bench-admin-token-1"),
    "ca_node": env("OTA_CA_NODE", HOME + "/bench-ca.pem"),
    "ca_board": env("OTA_CA_BOARD", "/flash/bench-ca.pem"),
    "wifi_ssid": env("WIFI_SSID", ""),
    "wifi_pass": env("WIFI_PASSWORD", ""),
    "project": env("PROJECT_DIR", HOME + "/proj"),
    "venv": env("OTA_VENV", HOME + "/ota-venv"),
    "sdk": env("SDK_HOME", HOME + "/openmv-sdk-1.6.0"),
    "jlink": env("JLINK", HOME + "/jlink/JLinkExe"),
    "dfu": env("DFU_UTIL", HOME + "/openmv-sdk-1.6.0/bin/dfu-util"),
    "blhost": env("BLHOST", HOME + "/openmv-sdk-1.6.0/python/bin/blhost"),
    "acm": env("BOARD_ACM", "/dev/ttyACM0"),
    "uart": env("BOARD_UART", "/dev/ttyUSB0"),
    # the update server's local artifact store, for tamper scenarios (corrupt/bad_sig) --
    # only reachable when the harness runs ON the server node (co-located store).
    "artifacts": env("OTA_ARTIFACTS", HOME + "/otasrv/artifacts"),
}

# Per-board: which side-channel UART carries markers, how it reaches the network, and
# how the golden image is flashed. Kept data-driven so a new board is one entry.
BOARDS = {
    "OPENMV_N6": {
        "cov_uart": 3,                       # UART(3) on P4/P5
        "cov_write": "install.xip",          # this board's write path (block-dev boards differ)
        "network": "lan",
        "flash": "jlink_stm32",
        "jlink_device": "STM32N657L0",
        "fw_addr": "0x70080000",
        "romfs_addr": "0x70800000",
        "fw_bin": "fw/build/OPENMV_N6/bin/firmware.bin",
    },
    "OPENMV_AE3": {
        "cov_uart": 1,                       # UART(1) on P4/P5
        "cov_write": "install.xip",
        "network": "wifi",
        "flash": "dfu_alif",
        "romfs_alt": "6",                    # external OSPI romfs partition
    },
    "OPENMV_RT1060": {
        "cov_uart": 1,                       # UART(1) on P4/P5
        "cov_write": "install.blockdev",     # mimxrt: the block-device write model, not XIP
        "network": "lan",
        "flash": "blhost_imx",
        # The FlexSPI NOR flash. We write ONLY the app regions; the ROM's flash-config
        # block (0x60000000) and the resident secure bootloader / flashloader (0x60001000)
        # are NEVER touched -- machine.bootloader() drops into that resident SBL to flash.
        "fw_addr": "0x60040000",
        "romfs_addr": "0x60800000",
        "romfs_size": "0x800000",            # the whole dual-slot romfs region (for no_slot brick)
        "blhost_usb": "0x15A2,0x0073",       # the MCU-bootloader (blhost) device the SBL exposes
        "blhost_lsusb": "15a2:0073",         # ...same, as lsusb prints it (for the enumerate poll)
        "cfg_addr": "0x2000",                # FlexSPI config option word + apply target
        "cfg_spi": "0xC0000008",
        "cfg_type": "9",
    },
}


def ota(name):
    return CFG["venv"] + "/bin/" + name


def sh(cmd, timeout=180, check=True, quiet=False):
    """Run a command, returning (rc, stdout+stderr). Never raises on non-zero unless check."""
    if not quiet:
        log("$ " + (cmd if isinstance(cmd, str) else " ".join(cmd)))
    p = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True,
                       timeout=timeout)
    out = (p.stdout or "") + (p.stderr or "")
    if check and p.returncode != 0:
        raise RuntimeError("command failed (%d): %s\n%s" % (p.returncode, cmd, out[-2000:]))
    return p.returncode, out


def device_exec(code, timeout=60, check=True):
    """Run MicroPython on the board over the USB-CDC (mpremote). Opening the port
    DTR-resets the board, so this is only for setup/verify, never for observing a trial."""
    return sh([ota("mpremote"), "connect", CFG["acm"], "exec", code], timeout=timeout,
              quiet=True, check=check)


def log(msg):
    print("[hil] " + msg, flush=True)


# The coverage checklist: an OTA code path is "covered" when its (stable) log line shows
# up on the UART. Keyed on a substring so the timestamp/level prefix and args don't matter.
# These are the SAME lines the device logs normally -- the bench just captures them at DEBUG.
# Update this when a path's log wording changes (the one coupling we accept for a single,
# no-special-markers logging channel).
COVERAGE = {
    "boot: mounted FRONT": "boot.mount.front",
    # BACK is only ever mounted as a FALLBACK (FRONT rejected), which boot.py logs as one
    # line -- "boot: FRONT rejected (<reason>) -> mounted BACK ..." -- so key on that tail,
    # not a standalone "boot: mounted BACK" line (which never occurs).
    "-> mounted BACK": "boot.mount.back",
    "boot: FRONT rejected": "boot.front_reject",
    "boot: no bootable slot": "boot.no_slot",
    "install: erasing FRONT": "install.start",
    "install: write path block-device": "install.blockdev",
    "install: write path XIP": "install.xip",
    "install: representation delta": "install.delta",
    "install: representation full": "install.full",
    "install: attempt": "install.retry",
    "install: installed + armed": "install.armed",
    "install: FAILED after": "install.fallback",
    "install: rejected before erase": "install.reject",
    "checkin: response received": "run.checkin",
    "checkin: update offered": "run.offer",
    "confirm: kept running FRONT": "confirm.promoted",
}


# ---------------------------------------------------------------------------
# The scenario catalog. Each entry drives the conditions that make its code paths run and
# declares what it MUST cover ("expect") and what it must NOT ("forbid"), plus how it ends:
#   end="promoted" -- device installs, trials, confirms, and promotes to the target version
#                     on FRONT (the happy paths).
#   end="golden"   -- device stays on / falls back to the golden (the negative paths): the
#                     update is refused pre-erase, or installs then rolls back / falls back.
# "publish" picks how the update is produced (see publish_update); "app" picks the bench app
# variant (see bench_main_py). "{cov_write}" resolves per-board to install.xip / .blockdev.
# A run PASSES iff the end state matches AND every expect marker fired AND no forbid marker
# did -- so a dropped/renamed log line, or a safety path that silently stops running, fails.
# The union of every scenario's expect set is the full COVERAGE matrix (bar boot.no_slot,
# which needs both slots bricked -- too destructive to trigger on real hardware).
SCENARIOS = {
    "delta": {
        "desc": "happy path: delta install -> trial -> confirm -> promote",
        "publish": "delta", "app": "confirm", "end": "promoted",
        "expect": ["boot.mount.front", "run.checkin", "run.offer", "install.start",
                   "{cov_write}", "install.delta", "install.armed", "confirm.promoted"],
        "forbid": ["install.full", "install.fallback", "install.reject", "boot.mount.back"],
    },
    "full": {
        "desc": "full (non-delta) image install -> trial -> confirm -> promote",
        "publish": "full", "app": "confirm", "end": "promoted",
        "expect": ["boot.mount.front", "run.offer", "install.start",
                   "{cov_write}", "install.full", "install.armed", "confirm.promoted"],
        "forbid": ["install.delta", "install.fallback", "install.reject"],
    },
    "corrupt": {
        "desc": "tampered image fails integrity -> retries exhausted -> golden BACK",
        "publish": "corrupt", "app": "confirm", "end": "golden",
        "expect": ["install.start", "install.retry", "install.fallback",
                   "boot.front_reject", "boot.mount.back"],
        "forbid": ["install.armed", "confirm.promoted"],
    },
    "rollback": {
        "desc": "trial never confirms -> next boot rejects FRONT -> golden BACK",
        "publish": "delta", "app": "no_confirm", "end": "golden",
        "expect": ["install.armed", "boot.front_reject", "boot.mount.back"],
        "forbid": ["confirm.promoted"],
    },
    "bad_sig": {
        "desc": "manifest signed by an untrusted key -> refused pre-erase, stays golden",
        "publish": "bad_sig", "app": "confirm", "end": "golden",
        "expect": ["run.offer", "install.reject"],
        "forbid": ["install.start", "install.armed", "confirm.promoted", "boot.mount.back"],
    },
    "bad_version": {
        "desc": "version <= anti-rollback floor -> device refuses pre-erase, stays golden",
        # A full image (not a delta): a delta must go golden->newer, but here the release is
        # OLDER than golden -- the device rejects it at the version check, before rep selection.
        "publish": "full", "app": "confirm", "end": "golden", "version": "0.9.0",
        "expect": ["run.offer", "install.reject"],
        "forbid": ["install.start", "install.armed", "confirm.promoted", "boot.mount.back"],
        # NEEDS the bench server started with test_offer_downgrades on
        # (OPENMV_OTA_TEST_OFFER_DOWNGRADES=1). A correct server never OFFERS a release <= a
        # device's current version (its own anti-rollback), so the device's anti-rollback --
        # the real safety boundary -- can't otherwise be reached on hardware. The flag relaxes
        # only the server's OFFER; the device still rejects the downgrade (what we're testing).
    },
    "no_slot": {
        "desc": "both romfs slots invalid -> boot finds nothing bootable (the brick floor)",
        # No OTA: erase BOTH slots (the whole romfs region) on an otherwise-provisioned board --
        # firmware + /flash/.hilcov_uart stay intact -- reset, and watch boot.py fail to mount
        # anything. RUN AFTER another scenario (the board must be bootable so it still has the
        # bench logger + the coverage-UART file). Flash-only; block-device (RT1062) for now.
        "publish": "none", "app": "confirm", "end": "no_slot",
        "expect": ["boot.no_slot"],
        # NOT boot.mount.front: entering the SBL via machine.bootloader() boots the (still-valid)
        # golden ONCE before blhost erases it, so a FRONT mount precedes the brick -- expected.
        # Forbid the things that prove the device is genuinely bricked if ABSENT: it ran no app
        # (no check-in) and did no OTA.
        "forbid": ["run.checkin", "install.start", "confirm.promoted"],
    },
}


def scenario_markers(board, key):
    """(expect, forbid) marker sets for a scenario, with {cov_write} resolved per board."""
    def resolve(names):
        return {BOARDS[board]["cov_write"] if n == "{cov_write}" else n for n in names}
    s = SCENARIOS[key]
    return resolve(s["expect"]), resolve(s["forbid"])


# ---------------------------------------------------------------------------
# UART marker capture -- a background reader that records every HILCOV line for the
# whole cycle, independent of the USB-CDC console and surviving every reboot.
# ---------------------------------------------------------------------------
class UartCapture:
    def __init__(self, port, baud=115200):
        import serial
        self._ser = serial.Serial(port, baud, timeout=0.5)
        self._ser.reset_input_buffer()
        self.markers = []                    # ordered (t, point)
        self.raw = []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def start(self, t0):
        self._t0 = t0
        self._t.start()

    def _run(self):
        buf = b""
        while not self._stop.is_set():
            try:
                buf += self._ser.read(256)
            except Exception:
                continue
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                s = line.decode("utf-8", "replace").strip()
                if not s:
                    continue
                self.raw.append(s)
                # Coverage = the device's own log lines (captured at DEBUG on the UART):
                #   "[  12.345] INFO openmv_ota: install: representation delta"
                # No break: the last line before a machine.reset() can be truncated and
                # concatenated with the next boot's line, so one captured line may carry
                # more than one marker -- record them all.
                for sub, cid in COVERAGE.items():
                    if sub in s:
                        self.markers.append((round(time.time() - self._t0, 1), cid))

    def points(self):
        return sorted({p for _, p in self.markers})

    def stop(self):
        self._stop.set()
        self._t.join(timeout=2)
        try:
            self._ser.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Bench app (main.py) -- minimal: bring the network up, run the OTA loop against the
# bench server, and confirm the trial once operational. LAN or WiFi per board.
# ---------------------------------------------------------------------------
def bench_main_py(board, net, app="confirm"):
    if net == "wifi":
        bring_up = (
            'wl = network.WLAN(network.STA_IF)\n'
            '    wl.active(True)\n'
            '    if not wl.isconnected():\n'
            '        wl.connect(%r, %r)\n'
            '        while not wl.isconnected():\n'
            '            await asyncio.sleep_ms(200)\n'
            '    print("BENCH up", wl.ifconfig()[0])\n' % (CFG["wifi_ssid"], CFG["wifi_pass"])
        )
    else:
        bring_up = (
            'lan = network.LAN()\n'
            '    lan.active(True)\n'
            '    while not lan.isconnected():\n'
            '        await asyncio.sleep_ms(200)\n'
            '    print("BENCH up", lan.ifconfig()[0])\n'
        )
    # The trial policy is the app's job (run() never auto-confirms), so it's the knob the
    # scenarios turn. "confirm": promote the trial once operational (the normal deploy).
    # "no_confirm": a trial that never becomes healthy -- do NOT confirm, wait, then reset,
    # so the next boot rejects the un-confirmed FRONT and falls back to golden (the anti-brick
    # / rollback path). status().trial is only true on a freshly-installed trial boot, so the
    # golden boot that DOES the install is unaffected either way.
    if app == "no_confirm":
        trial_policy = (
            "    st = openmv_ota.status()\n"
            "    if st.get('trial'):\n"
            "        _blog.warning('app: trial NOT confirming (rollback scenario); reset in 15s')\n"
            "        await asyncio.sleep(15)\n"
            "        import machine\n"
            "        machine.reset()\n"
            "    while True:\n"
            "        await asyncio.sleep(2)\n"
        )
    else:
        trial_policy = (
            "    confirmed = False\n"
            "    while True:\n"
            "        if not confirmed:\n"
            "            confirmed = True\n"
            "            openmv_ota.confirm()\n"
            "        await asyncio.sleep(2)\n"
        )
    # The app logs its OWN progress to the openmv_ota logger (-> the coverage UART at DEBUG),
    # and wraps the whole run so any crash is VISIBLE there. Without this a trial that boots
    # but faults in the app is invisible: an uncaught exception prints to the USB REPL, not
    # the UART, so a corrupt-trial hang looks identical to a silent network stall.
    return (
        "import asyncio\n"
        "import logging\n"
        "import sys\n"
        "import network\n"
        "import openmv_ota\n"
        "_blog = logging.getLogger('openmv_ota')\n\n\n"
        "async def main():\n"
        "    _blog.info('app: main() started')\n"
        "    " + bring_up +
        "    _blog.info('app: network up, starting run()')\n"
        "    asyncio.create_task(openmv_ota.run(%r, ca=%r, poll_after_s=5))\n" % (
            CFG["server"], CFG["ca_board"]) +
        trial_policy + "\n\n"
        "try:\n"
        "    _blog.info('app: booting ' + str(openmv_ota.status().get('version')))\n"
        "    asyncio.run(main())\n"
        "except Exception as e:\n"
        "    _blog.error('app: CRASHED %r' % (e,))\n"
        "    sys.print_exception(e)\n"
    )


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------
def _ver(v):
    return tuple(int(x) for x in v.split("."))


def set_version(v):
    p = CFG["project"] + "/app/settings.json"
    d = json.load(open(p))
    d["app_version"] = v
    # The project's BUILD-time rollback floor can't exceed the version being built, so a
    # bad_version publish (0.9.0) needs it lowered. This is only the build's sanity gate -- the
    # DEVICE's own floor (baked into the flashed golden's BACK slot) is what a bad_version run
    # actually tests, and that stays at 1.0.0, so the device still rejects the 0.9.0 offer.
    floor = d.get("rollback_floor", "1.0.0")
    d["rollback_floor"] = v if _ver(v) < _ver(floor) else floor
    json.dump(d, open(p, "w"), indent=2)


def prepare(board, checkout, network, app="confirm"):
    log("prepare: install checkout + refresh vendored runtime + bench app")
    sh([ota("pip"), "install", "-q", "-e", checkout], timeout=300)
    dev = checkout + "/src/openmv_ota/build/device"
    # The project VENDORS its own copies -- the build reads those, not the package: the
    # romfs app lib (openmv_ota/openmv_cloud) AND the frozen survival modules in device/
    # (openmv_log/openmv_wdt/openmv_rtc). Refresh both so the run tests the checkout.
    sh("cp -rf %s/openmv_ota/. %s/app/lib/openmv_ota/" % (dev, CFG["project"]))
    sh("cp -rf %s/openmv_cloud/. %s/app/lib/openmv_cloud/ 2>/dev/null || true" % (dev, CFG["project"]))
    sh("mkdir -p %s/device && cp -f %s/*.py %s/device/" % (CFG["project"], dev, CFG["project"]))
    open(CFG["project"] + "/app/main.py", "w").write(bench_main_py(board, network, app))
    # the bench server's CA must be on the board for run()'s TLS (survives the OTA, lives on
    # /flash not the romfs). Push it so the harness doesn't assume a hand-placed cert.
    if os.path.exists(CFG["ca_node"]):
        sh([ota("mpremote"), "connect", CFG["acm"], "fs", "cp", CFG["ca_node"],
            ":" + CFG["ca_board"]], timeout=30, check=False)
    # enable the coverage UART on the board (bench-only file; survives across the OTA)
    device_exec("f=open(%r,'w');f.write('%d');f.close()" % (CFG["ca_board"].rsplit("/", 1)[0] +
                "/.hilcov_uart", BOARDS[board]["cov_uart"]))


def build_golden(board):
    log("build: firmware + factory-romfs (golden 1.0.0)")
    set_version("1.0.0")
    penv = dict(os.environ, PATH=CFG["sdk"] + "/make:" + os.environ["PATH"])
    for step in ("firmware", "factory-romfs"):
        extra = ["--allow-dev-key", "--no-account"] if step == "factory-romfs" else []
        subprocess.run([ota("openmv-ota"), "build", step, CFG["project"], "-b", board] + extra,
                       env=penv, check=True, timeout=900)


def flash_golden(board, bad_romfs=False):
    fn = globals()["_flash_" + BOARDS[board]["flash"]]
    fn(board, bad_romfs) if bad_romfs else fn(board)


def _flash_jlink_stm32(board, bad_romfs=False):
    if bad_romfs:
        raise RuntimeError("no_slot (bad_romfs) flash not implemented for %s yet" % board)
    b = BOARDS[board]
    build = CFG["project"] + "/build"
    img = "%s/%s-factory-romfs.img" % (build, board)
    binf = "%s/%s-factory-romfs.bin" % (build, board)   # J-Link loadbin needs a .bin extension
    sh("cp -f %s %s" % (img, binf))
    fw = os.path.join(HOME, b["fw_bin"])                 # ~/fw/build/<board>/bin/firmware.bin
    for name, addr, f in (("firmware", b["fw_addr"], fw), ("romfs", b["romfs_addr"], binf)):
        log("flash %s -> %s (J-Link)" % (name, addr))
        script = "\n".join(["device " + b["jlink_device"], "si SWD", "speed 4000", "connect",
                            "r", "h", "loadbin %s %s" % (f, addr), "r", "g", "exit"]) + "\n"
        sp = "/tmp/jl-%s.jlink" % name
        open(sp, "w").write(script)
        rc, out = sh([CFG["jlink"], "-nogui", "1", "-CommanderScript", sp], timeout=300, check=False)
        if "O.K." not in out or "unsupported" in out.lower():
            raise RuntimeError("J-Link %s flash failed:\n%s" % (name, out[-1500:]))


def _dfu_write(alt, path, timeout_s):
    """One DFU download to an alt setting, WITHOUT --reset (that hangs the AE3 after the
    write completes). Poll the piped output for 'Done!', then return -- the caller leaves
    DFU once. Raises if the write didn't finish."""
    logf = "/tmp/dfu_a%s.out" % alt
    proc = subprocess.Popen([CFG["dfu"], "-d", ",37c5:96e3", "-a", alt, "-D", path],
                            stdout=open(logf, "w"), stderr=subprocess.STDOUT)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(10)
        if "Done!" in open(logf, errors="replace").read() or proc.poll() is not None:
            break
    proc.kill()
    if "Done!" not in open(logf, errors="replace").read():
        raise RuntimeError("DFU alt %s write did not complete:\n%s" % (
            alt, open(logf, errors="replace").read()[-1500:]))


def _flash_dfu_alif(board, bad_romfs=False):
    if bad_romfs:
        raise RuntimeError("no_slot (bad_romfs) flash not implemented for %s yet" % board)
    b = BOARDS[board]
    build = CFG["project"] + "/build"
    fw = "%s/%s-firmware-M55_HP.bin" % (build, board)     # the main core (carries the frozen
    img = "%s/%s-factory-romfs.img" % (build, board)      # boot.py + openmv_log)
    rimg = "%s/%s-romfs.img" % (build, board)
    sh("cp -f %s %s" % (img, rimg))
    # One DFU session: firmware (MRAM alt 1, fast) THEN romfs (OSPI, ~10 min), then leave.
    log("flash: reset to DFU")
    device_exec("import machine; machine.bootloader()", timeout=30, check=False)
    time.sleep(6)
    log("flash firmware -> MRAM alt 1 (DFU)")
    _dfu_write("1", fw, 300)
    log("flash romfs -> OSPI alt %s (DFU, ~10 min)" % b["romfs_alt"])
    _dfu_write(b["romfs_alt"], rimg, 1200)
    sh([CFG["dfu"], "-d", ",37c5:96e3", "-a", b["romfs_alt"], "-e"], check=False, timeout=60)
    time.sleep(15)                           # the AE3 (Alif) takes longer to boot + re-enumerate


def _aligned(n, sector=0x1000):
    """Round up to the FlexSPI NOR erase granularity (erase-region needs a sector multiple)."""
    return (n + sector - 1) & ~(sector - 1)


def _wait_usb(lsusb_id, timeout_s):
    """Poll ``lsusb`` until a device with this ``vid:pid`` enumerates. Returns on success,
    raises on timeout. Used to catch the RT's resident SBL/blhost after machine.bootloader()."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        _rc, out = sh("lsusb", timeout=10, check=False, quiet=True)
        if lsusb_id.lower() in out.lower():
            return
        time.sleep(0.1)
    raise RuntimeError("USB device %s did not enumerate within %ss" % (lsusb_id, timeout_s))


def _blhost(usb, *sub, timeout_ms=None):
    argv = [CFG["blhost"], "-u", usb]
    if timeout_ms is not None:
        argv += ["-t", str(timeout_ms)]
    return argv + ["--", *sub]


def _blhost_run(label, usb, *sub, timeout_ms=None):
    """Run one blhost sub-command and require it reported success (blhost exits 0 even when
    the target NAKs, so we parse the response status, mirroring the JLink 'never trust the
    exit code' lesson)."""
    log("  blhost: " + label)
    _rc, out = sh(_blhost(usb, *sub, timeout_ms=timeout_ms), timeout=180, check=False, quiet=True)
    if "0 (0x0) Success" not in out:
        raise RuntimeError("blhost %s failed:\n%s" % (label, out[-1200:]))


def _enter_blhost(b):
    """Drop into the resident SBL's serial-download (blhost) mode and wait until blhost can
    actually talk to it, retrying the whole entry as needed. Two things fight us: the SBL
    idle-times-out back to runtime if no command arrives, and the /dev/hidraw node's group
    perms lag USB enumeration (so a too-eager open races udev). We settle briefly for udev,
    probe with get-property, and re-enter on any miss. Once we return, the caller must keep
    blhost busy (back-to-back commands) so the idle timeout never fires mid-provision."""
    usb = b["blhost_usb"]
    out = ""
    for attempt in range(6):
        # Fire the bootloader entry fire-and-forget: machine.bootloader() drops the USB-CDC,
        # so a synchronous mpremote would block on the dead port's teardown -- and every idle
        # second risks the SBL timing back out to runtime. Backgrounded, mpremote connects +
        # sends the call (~1-2s) while we poll for the blhost device to appear.
        subprocess.Popen([ota("mpremote"), "connect", CFG["acm"], "exec",
                          "import machine; machine.bootloader()"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            _wait_usb(b["blhost_lsusb"], timeout_s=15)
        except RuntimeError:
            continue                                     # never enumerated -> re-issue bootloader
        # Fire blhost IMMEDIATELY -- no perm settle needed (the 0666 hidraw udev rule sets
        # access at node creation), and the SBL idle-times-out fast, so don't dawdle.
        _rc, out = sh(_blhost(usb, "get-property", "1"), timeout=30, check=False, quiet=True)
        if "0 (0x0) Success" in out:
            return
        tail = (out.strip().splitlines() or ["?"])[-1][:90]
        log("  (blhost entry retry %d/6: %s)" % (attempt + 1, tail))
        time.sleep(1)
    raise RuntimeError("could not reach the SBL/blhost after 6 tries:\n" + out[-800:])


def _flash_blhost_imx(board, bad_romfs=False):
    """Provision golden on the mimxrt (RT1062): drop into the resident SBL via
    machine.bootloader() (no SBL jumper -- that's only for restoring a wiped bootloader),
    then drive blhost to (re)write ONLY the firmware + romfs regions of the FlexSPI NOR.
    The FCB (0x60000000) and the SBL/flashloader (0x60001000) are left untouched.

    bad_romfs=True is the no_slot brick: ERASE the whole romfs region (both slots -> blank ->
    no valid trailer in either) and leave firmware + /flash untouched, so boot.py runs (bench
    logger intact) and finds nothing bootable. No firmware/romfs write."""
    b = BOARDS[board]
    build = CFG["project"] + "/build"
    fw = "%s/%s-firmware.bin" % (build, board)          # self-contained (its own FCB+IVT+app)
    romfs = "%s/%s-factory-romfs.img" % (build, board)
    usb = b["blhost_usb"]
    log("flash: reset into the resident SBL (blhost)%s" % (" [no_slot brick]" if bad_romfs else ""))
    _enter_blhost(b)                                     # ...and keep it busy from here on
    _blhost_run("configure FlexSPI NOR", usb, "fill-memory", b["cfg_addr"], "4", b["cfg_spi"], "word")
    _blhost_run("apply FlexSPI config", usb, "configure-memory", b["cfg_type"], b["cfg_addr"])
    if bad_romfs:
        length = b["romfs_size"]                             # the whole dual-slot region
        log("brick: erase romfs -> %s (%s), no write (both slots blank)" % (b["romfs_addr"], length))
        _blhost_run("erase romfs %s" % length, usb, "flash-erase-region", b["romfs_addr"], length,
                    timeout_ms=120000)
    else:
        for name, addr, f in (("firmware", b["fw_addr"], fw), ("romfs", b["romfs_addr"], romfs)):
            length = "0x%X" % _aligned(os.path.getsize(f))
            log("flash %s -> %s (%s, blhost)" % (name, addr, length))
            _blhost_run("erase %s %s" % (name, length), usb, "flash-erase-region", addr, length,
                        timeout_ms=120000)
            _blhost_run("write %s" % name, usb, "write-memory", addr, f)
    _blhost_run("reset", usb, "reset")
    time.sleep(12)                                       # POR + FlexSPI re-enumerate as runtime


def verify_golden():
    log("verify: golden boots + /rom mounts + main.py present (uncompiled)")
    last = ""
    for _ in range(8):                       # the board may still be (re)booting after a flash
        time.sleep(5)
        try:
            _rc, last = device_exec(
                'import os; r=os.listdir("/"); '
                'print("ROMOK", ("rom" in r) and ("main.py" in os.listdir("/rom")))',
                timeout=30, check=False)
            if "ROMOK True" in last:
                return
        except Exception as e:
            last = str(e)
    raise RuntimeError("golden did not mount a valid romfs:\n" + last)


def publish_update(board, version, variant="delta"):
    log("publish: %s (variant=%s, rollout 100%%)" % (version, variant))
    set_version(version)
    penv = dict(os.environ, PATH=CFG["sdk"] + "/make:" + os.environ["PATH"],
                SSL_CERT_FILE=CFG["ca_node"])
    # --allow-republish: the bench server accumulates versions across runs, so this
    # target may not be strictly newer than a prior run's -- the device is what gates
    # (it re-flashes to golden 1.0.0 each run, and its rollback floor resets with it).
    build = [ota("openmv-ota"), "build", "ota-romfs", CFG["project"], "-b", board,
             "--allow-dev-key", "--allow-republish"]
    if variant == "full":
        # Force a full (non-delta) release: point --delta-from at an empty dir so no golden
        # resolves (build_ota_romfs -> "full image only"), and the device installs the full rep.
        nodelta = tempfile.mkdtemp(prefix="hil-nodelta-")
        build += ["--delta-from", nodelta]
        # A full-only build does NOT produce a .delta.gz, but a prior delta build left one in
        # the build dir -- and `client publish` uploads every artifact present, so the server
        # rejects (delta uploaded, manifest declares none). Drop the stale delta first.
        sh("rm -f %s/build/%s-ota.delta.gz" % (CFG["project"], board), check=False)
    subprocess.run(build, env=penv, check=True, timeout=900)
    subprocess.run([ota("openmv-ota"), "client", "publish", CFG["project"], "-b", board,
                    "--server", CFG["server"], "--token", CFG["token"], "--allow-republish",
                    "--rollout", "__default__:100"], env=penv, check=True, timeout=180)
    if variant == "corrupt":
        _tamper(board, "image")        # post-erase integrity failure -> retry -> golden BACK
    elif variant == "bad_sig":
        _tamper(board, "manifest")     # pre-erase signature failure -> reject, stays golden


def _tamper(board, which):
    """Flip a byte in the JUST-published artifact in the LOCAL server store, to exercise a
    device integrity path that a clean release can't:
      which="image"    -> the offered .delta.gz/.img.gz: the download decompress/sha256 fails
                          AFTER the FRONT erase commits -> retries exhaust -> reboot to golden.
      which="manifest" -> the manifest.bin: its signature no longer covers the mutated bytes
                          -> the device refuses it BEFORE erasing -> stays on golden.
    Needs the harness to run ON the server node (the artifact store is local); raises loudly
    otherwise so a tamper scenario can't silently degrade into a clean install."""
    import glob
    root = CFG["artifacts"]
    imgs = sorted(glob.glob("%s/artifacts/rel_*/%s-ota.*.gz" % (root, board)),
                  key=os.path.getmtime)
    if not imgs:
        raise RuntimeError("no published artifact for %s under %s -- tamper scenarios need the "
                           "harness on the server node (co-located store)" % (board, root))
    newest = imgs[-1]                                    # the release we just published
    rel = os.path.basename(os.path.dirname(newest))      # rel_<id>, shared by image + manifest
    if which == "manifest":
        target = "%s/manifests/%s/manifest.bin" % (root, rel)
    else:
        # prefer the delta blob (the device picks it over full when the base matches)
        deltas = [p for p in imgs if os.path.dirname(p).endswith(rel) and p.endswith(".delta.gz")]
        target = deltas[-1] if deltas else newest
    with open(target, "r+b") as f:
        f.seek(0, 2)
        n = f.tell()
        mid = n // 2                                     # flip one byte mid-stream
        f.seek(mid)
        b = f.read(1)
        f.seek(mid)
        f.write(bytes([b[0] ^ 0xFF]))
    log("  tampered %s byte@%d of %s" % (which, mid, os.path.basename(target)))


def device_record():
    """All device records from the server admin API."""
    import urllib.request
    import ssl
    ctx = ssl.create_default_context(cafile=CFG["ca_node"])
    try:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    except Exception:
        pass
    req = urllib.request.Request(CFG["server"] + "/api/v1/admin/devices",
                                 headers={"Authorization": "Bearer " + CFG["token"]})
    data = json.load(urllib.request.urlopen(req, context=ctx, timeout=15))
    items = data if isinstance(data, list) else data.get("devices", data.get("items", []))
    # the newest-installed record for this product is the one whose version moves; return all
    return items


def device_id():
    """This unit's hardware id (matches the server's device record), read off the board."""
    rc, out = device_exec('import openmv_ota; print("DEVID", openmv_ota.identity().get("device_id"))')
    for line in out.splitlines():
        if line.startswith("DEVID "):
            return line.split(" ", 1)[1].strip()
    raise RuntimeError("could not read device_id:\n" + out)


def run_cycle_no_slot(cap, expect, timeout_s):
    """The no_slot watcher: both romfs slots are already bricked (the brick flash reset the
    board into boot.py). There is no server traffic -- the device can't mount /rom, so it never
    checks in -- we only watch the UART for boot.py's 'no bootable slot'. Re-reset once via the
    REPL (boot.py failing still leaves the USB console up) in case the first boot's line landed
    before capture was ready. PASS = the marker appears."""
    log("cycle: bricked both slots -> watching UART for 'no bootable slot'")
    reset_tried = False
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(5)
        if expect <= set(cap.points()):
            break
        if not reset_tried and time.time() - (deadline - timeout_s) > 20:
            reset_tried = True                   # nudge a fresh boot if we didn't catch the first
            try:
                device_exec("import machine; machine.reset()", timeout=20, check=False)
            except Exception:
                pass
    return {"saw_golden": False, "saw_target": False, "version": None, "slot": None,
            "reached_end": expect <= set(cap.points())}


def run_cycle(devid, golden, target, end, expect, cap, timeout_s):
    """Hard-reset the device and watch the server record + UART until the scenario's end
    state is reached (early exit) or the timeout elapses. Returns the observed state; the
    caller decides PASS/FAIL against the scenario's expect/forbid sets.

    end="promoted": the device ends on the TARGET, confirmed on FRONT (the happy paths).
    end="golden":   the device stays on / falls back to the GOLDEN (the negative paths -- an
                    update refused pre-erase, or installed then rolled back / fell back). The
                    device may loop (re-offer -> re-fail -> golden), so we exit once it has
                    SETTLED back on golden with every expected marker seen."""
    log("cycle: hard reset -> autonomous run; end=%s; watching UART + server" % end)
    try:                                     # machine.reset() drops the USB-CDC -> mpremote
        device_exec("import machine; machine.reset()", timeout=20, check=False)
    except Exception:
        pass                                 # ...an I/O error here just means the reset landed
    deadline = time.time() + timeout_s
    last = None
    saw_golden = saw_target = False
    v = slot = None
    while time.time() < deadline:
        time.sleep(15)
        try:
            recs = device_record()
        except Exception as e:
            log("  (server query retry: %s)" % e)
            continue
        me = [r for r in recs if r.get("device_id") == devid]
        v = me[0].get("current_version") if me else None
        slot = me[0].get("slot") if me else None
        if v == golden:                      # the freshly re-flashed golden checked in
            saw_golden = True
        if v == target:
            saw_target = True
        marks = set(cap.points())
        have = expect <= marks
        cur = "%s/%s golden=%s markers=[%s]" % (v, slot, saw_golden, ",".join(sorted(marks)))
        if cur != last:
            log("  device " + devid[:12] + ": " + cur)
            last = cur
        if end == "promoted":
            if saw_golden and v == target and slot == "FRONT" and have:
                break                        # real golden->target transition, all paths hit
        elif saw_golden and v == golden and have:
            break                            # settled back on golden, all negative paths hit
    reached = ((end == "promoted" and saw_golden and v == target and slot == "FRONT")
               or (end == "golden" and saw_golden and v == golden))
    return {"saw_golden": saw_golden, "saw_target": saw_target,
            "version": v, "slot": slot, "reached_end": reached}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", required=True, choices=sorted(BOARDS))
    ap.add_argument("--checkout", default=env("GITHUB_WORKSPACE", os.getcwd()))
    ap.add_argument("--target", default="1.1.0", help="the update version to install")
    ap.add_argument("--timeout", type=int, default=int(env("HIL_TIMEOUT", "600")))
    ap.add_argument("--trace", default=env("HIL_TRACE", "hil-trace.json"))
    ap.add_argument("--network", choices=["lan", "wifi"], default=None,
                    help="override the board's default network for the bench app (e.g. N6 wifi)")
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), default="delta",
                    help="which OTA path to exercise (see SCENARIOS): delta/full happy paths, "
                         "corrupt/rollback/bad_sig/bad_version negative paths")
    ap.add_argument("--skip-provision", action="store_true",
                    help="reuse the already-flashed golden (skip build/flash/verify). Use WITH "
                         "--skip-publish: a fresh publish rebuilds the golden, and a delta's base "
                         "must match the flashed golden or the install fails the sha256 check.")
    ap.add_argument("--skip-publish", action="store_true",
                    help="reuse the already-published update")
    args = ap.parse_args()

    network = args.network or BOARDS[args.board]["network"]
    spec = SCENARIOS[args.scenario]
    expect, forbid = scenario_markers(args.board, args.scenario)
    pub_version = spec.get("version", args.target)     # bad_version publishes below the floor
    t0 = time.time()
    trace = {"board": args.board, "network": network, "scenario": args.scenario,
             "target": args.target, "end": spec["end"], "passed": False,
             "expect": sorted(expect), "forbid": sorted(forbid), "markers": [], "phases": {}}
    cap = None

    def phase(name, fn):
        s = time.time()
        fn()
        trace["phases"][name] = round(time.time() - s, 1)

    try:
        log("board %s, network %s, scenario %s (%s)"
            % (args.board, network, args.scenario, spec["desc"]))
        if spec["end"] == "no_slot":
            # No OTA: brick BOTH romfs slots, then watch for boot.py's 'no bootable slot'. Start
            # capture BEFORE the brick flash so the reset it triggers (-> boot -> the log line)
            # is caught. Requires the board already provisioned + bootable (firmware carries the
            # bench logger and /flash/.hilcov_uart is set) -- run it after another scenario.
            cap = UartCapture(CFG["uart"])
            cap.start(time.time())
            phase("flash_brick", lambda: flash_golden(args.board, bad_romfs=True))
            result = run_cycle_no_slot(cap, expect, args.timeout)
        else:
            if not args.skip_provision:
                phase("prepare", lambda: prepare(args.board, args.checkout, network, spec["app"]))
                phase("build_golden", lambda: build_golden(args.board))
                phase("flash_golden", lambda: flash_golden(args.board))
                phase("verify_golden", verify_golden)
            devid = device_id()
            trace["device_id"] = devid
            log("device_id: " + devid)
            if not args.skip_publish:
                phase("publish", lambda: publish_update(args.board, pub_version, spec["publish"]))
            cap = UartCapture(CFG["uart"])
            cap.start(time.time())
            result = run_cycle(devid, "1.0.0", args.target, spec["end"], expect, cap, args.timeout)
        time.sleep(2)                            # let the last UART lines land
        marks = set(cap.points())
        missing = sorted(expect - marks)
        forbidden = sorted(forbid & marks)
        trace["result"] = result
        trace["missing_expected"] = missing
        trace["forbidden_hit"] = forbidden
        # PASS = the scenario reached its declared end state, hit EVERY expected path, and hit
        # NONE of the forbidden ones. So a dropped/renamed log line (missing), a safety path
        # that stopped running (missing), or a wrong path firing (forbidden) all fail the run.
        trace["passed"] = result["reached_end"] and not missing and not forbidden
        if not trace["passed"]:
            log("FAIL: end=%s reached=%s missing=%s forbidden=%s"
                % (spec["end"], result["reached_end"], missing or "-", forbidden or "-"))
    except Exception as e:
        trace["error"] = str(e)
        log("ERROR: " + str(e))
    finally:
        if cap is not None:
            cap.stop()
            trace["markers"] = cap.points()
            trace["missed"] = sorted(set(COVERAGE.values()) - set(cap.points()))
            trace["marker_trace"] = cap.markers
            trace["log"] = cap.raw                # the full device log for this run
        trace["elapsed_s"] = round(time.time() - t0, 1)
        json.dump(trace, open(args.trace, "w"), indent=2)

    log("=" * 60)
    log("RESULT: %s  scenario=%s  (%.0fs)"
        % ("PASS" if trace["passed"] else "FAIL", args.scenario, trace["elapsed_s"]))
    log("coverage %d/%d: %s" % (len(trace["markers"]), len(COVERAGE), ", ".join(trace["markers"])))
    log("trace -> " + args.trace)
    return 0 if trace["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

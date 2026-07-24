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
    "acm": env("BOARD_ACM", "/dev/ttyACM0"),
    "uart": env("BOARD_UART", "/dev/ttyUSB0"),
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
    "boot: mounted BACK": "boot.mount.back",
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
    "checkin: response received": "run.checkin",
    "checkin: update offered": "run.offer",
    "confirm: kept running FRONT": "confirm.promoted",
}


# NOTE -- adding the next scenario. Today there is ONE scenario: the happy-path DELTA
# install, whose required paths are expected_coverage() below. As we add scenarios, give
# each its OWN expected set and select it (e.g. a --scenario arg or a SCENARIOS dict), and
# have the scenario drive the conditions that make its paths run:
#   * full-image  -> publish a full (non-delta) release / device with no matching base
#                    => expects install.full instead of install.delta
#   * retry       -> kill the download mid-stream (drop the connection once)
#                    => expects install.retry (then install.armed on the retry)
#   * rollback    -> publish an update whose trial self-test fails (or never confirms)
#                    => expects boot.front_reject / boot.mount.back, NOT confirm.promoted
#   * block-device (RT1062) -> expects install.blockdev instead of install.xip
# Every marker in COVERAGE above should be an expected path of SOME scenario, so the union
# of the scenarios' expected sets is the full matrix -- that's the "all paths hit" gate.
def expected_coverage(board):
    """The coverage points a happy-path DELTA install on this board MUST hit. Missing any
    means either the path did not run OR its log line drifted -- both fail the run, so a
    covered log line can't silently disappear without the HIL checklist being updated.
    (Other paths -- rollback, retry, full, the other write model -- belong to their own
    scenarios; this default scenario is not expected to hit them.)"""
    return {
        "boot.mount.front", "run.checkin", "run.offer", "install.start",
        BOARDS[board]["cov_write"], "install.delta", "install.armed", "confirm.promoted",
    }


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
def bench_main_py(board, net):
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
    return (
        "import asyncio\n"
        "import network\n"
        "import openmv_ota\n\n\n"
        "async def main():\n"
        "    confirmed = False\n"
        "    " + bring_up +
        "    asyncio.create_task(openmv_ota.run(%r, ca=%r, poll_after_s=5))\n" % (
            CFG["server"], CFG["ca_board"]) +
        "    while True:\n"
        "        if not confirmed:\n"
        "            confirmed = True\n"
        "            openmv_ota.confirm()\n"
        "        await asyncio.sleep(2)\n\n\n"
        "asyncio.run(main())\n"
    )


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------
def set_version(v):
    p = CFG["project"] + "/app/settings.json"
    d = json.load(open(p))
    d["app_version"] = v
    if "rollback_floor" not in d:
        d["rollback_floor"] = "1.0.0"
    json.dump(d, open(p, "w"), indent=2)


def prepare(board, checkout, network):
    log("prepare: install checkout + refresh vendored runtime + bench app")
    sh([ota("pip"), "install", "-q", "-e", checkout], timeout=300)
    dev = checkout + "/src/openmv_ota/build/device"
    # The project VENDORS its own copies -- the build reads those, not the package: the
    # romfs app lib (openmv_ota/openmv_cloud) AND the frozen survival modules in device/
    # (openmv_log/openmv_wdt/openmv_rtc). Refresh both so the run tests the checkout.
    sh("cp -rf %s/openmv_ota/. %s/app/lib/openmv_ota/" % (dev, CFG["project"]))
    sh("cp -rf %s/openmv_cloud/. %s/app/lib/openmv_cloud/ 2>/dev/null || true" % (dev, CFG["project"]))
    sh("mkdir -p %s/device && cp -f %s/*.py %s/device/" % (CFG["project"], dev, CFG["project"]))
    open(CFG["project"] + "/app/main.py", "w").write(bench_main_py(board, network))
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


def flash_golden(board):
    fn = globals()["_flash_" + BOARDS[board]["flash"]]
    fn(board)


def _flash_jlink_stm32(board):
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


def _flash_dfu_alif(board):
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


def publish_update(board, version):
    log("publish: %s (delta + full, rollout 100%%)" % version)
    set_version(version)
    penv = dict(os.environ, PATH=CFG["sdk"] + "/make:" + os.environ["PATH"],
                SSL_CERT_FILE=CFG["ca_node"])
    # --allow-republish: the bench server accumulates versions across runs, so this
    # target may not be strictly newer than a prior run's -- the device is what gates
    # (it re-flashes to golden 1.0.0 each run, and its rollback floor resets with it).
    subprocess.run([ota("openmv-ota"), "build", "ota-romfs", CFG["project"], "-b", board,
                    "--allow-dev-key", "--allow-republish"], env=penv, check=True, timeout=900)
    subprocess.run([ota("openmv-ota"), "client", "publish", CFG["project"], "-b", board,
                    "--server", CFG["server"], "--token", CFG["token"], "--allow-republish",
                    "--rollout", "__default__:100"], env=penv, check=True, timeout=180)


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


def run_cycle(devid, golden, target, cap, timeout_s):
    log("cycle: hard reset -> autonomous install/trial/confirm; watching UART + server")
    try:                                     # machine.reset() drops the USB-CDC -> mpremote
        device_exec("import machine; machine.reset()", timeout=20, check=False)
    except Exception:
        pass                                 # ...an I/O error here just means the reset landed
    deadline = time.time() + timeout_s
    last = None
    saw_golden = False
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
        cur = "%s/%s golden=%s markers=[%s]" % (v, slot, saw_golden, ",".join(cap.points()))
        if cur != last:
            log("  device " + devid[:12] + ": " + cur)
            last = cur
        # PASS only after a real golden->target transition this run, ending confirmed on FRONT.
        if saw_golden and v == target and slot == "FRONT":
            return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", required=True, choices=sorted(BOARDS))
    ap.add_argument("--checkout", default=env("GITHUB_WORKSPACE", os.getcwd()))
    ap.add_argument("--target", default="1.1.0", help="the update version to install")
    ap.add_argument("--timeout", type=int, default=int(env("HIL_TIMEOUT", "600")))
    ap.add_argument("--trace", default=env("HIL_TRACE", "hil-trace.json"))
    ap.add_argument("--network", choices=["lan", "wifi"], default=None,
                    help="override the board's default network for the bench app (e.g. N6 wifi)")
    ap.add_argument("--skip-provision", action="store_true",
                    help="reuse the already-flashed golden (skip build/flash/verify). Use WITH "
                         "--skip-publish: a fresh publish rebuilds the golden, and a delta's base "
                         "must match the flashed golden or the install fails the sha256 check.")
    ap.add_argument("--skip-publish", action="store_true",
                    help="reuse the already-published update")
    args = ap.parse_args()

    network = args.network or BOARDS[args.board]["network"]
    t0 = time.time()
    trace = {"board": args.board, "network": network, "target": args.target,
             "passed": False, "markers": [], "phases": {}}
    cap = None

    def phase(name, fn):
        s = time.time()
        fn()
        trace["phases"][name] = round(time.time() - s, 1)

    try:
        log("board %s, network %s, target %s" % (args.board, network, args.target))
        if not args.skip_provision:
            phase("prepare", lambda: prepare(args.board, args.checkout, network))
            phase("build_golden", lambda: build_golden(args.board))
            phase("flash_golden", lambda: flash_golden(args.board))
            phase("verify_golden", verify_golden)
        devid = device_id()
        trace["device_id"] = devid
        log("device_id: " + devid)
        if not args.skip_publish:
            phase("publish", lambda: publish_update(args.board, args.target))
        cap = UartCapture(CFG["uart"])
        cap.start(time.time())
        version_ok = run_cycle(devid, "1.0.0", args.target, cap, args.timeout)
        time.sleep(2)                            # let the last UART lines land
        expected = expected_coverage(args.board)
        missing = sorted(expected - set(cap.points()))
        trace["version_reached"] = version_ok
        trace["expected"] = sorted(expected)
        trace["missing_expected"] = missing
        # PASS requires BOTH: the device promoted the update AND every expected path was
        # logged (a dropped/renamed coverage line -> missing_expected -> FAIL, by design).
        trace["passed"] = version_ok and not missing
        if version_ok and missing:
            log("FAIL: promoted %s but missing expected coverage: %s"
                % (args.target, ", ".join(missing)))
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
    log("RESULT: %s  (%.0fs)" % ("PASS" if trace["passed"] else "FAIL", trace["elapsed_s"]))
    log("coverage %d/%d: %s" % (len(trace["markers"]), len(COVERAGE), ", ".join(trace["markers"])))
    if trace.get("missed"):
        log("not covered this run: " + ", ".join(trace["missed"]))
    log("trace -> " + args.trace)
    return 0 if trace["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

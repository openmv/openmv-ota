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

    (the update server + its URL/token/store/CA are owned by ci/hil/bench_server -- each run
     spins up its OWN ephemeral server on the node, so no OTA_SERVER/OTA_TOKEN knob exists)
    OTA_CA_BOARD    CA path ON THE BOARD             (default /rom/bench-ca.pem)
    WIFI_SSID/WIFI_PASSWORD   for WiFi boards
    PROJECT_DIR     the pegged project on the node   (default ~/proj)
    OTA_VENV, SDK_HOME, JLINK, DFU_UTIL, MPREMOTE    tool paths (sensible defaults)
    BOARD_ACM       board USB-CDC serial             (default /dev/ttyACM0)
    BOARD_UART      the P4/P5 UART bridge on the node (default /dev/ttyUSB0)

This is a live-hardware gate, not a host unit test -- it is invoked by the
``hil-ota`` workflow (workflow_dispatch), never per-commit.
"""

import argparse
import glob
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

import bench_server                          # ephemeral per-run update server (sibling module)

HOME = os.path.expanduser("~")


def env(name, default):
    return os.environ.get(name, default)


CFG = {
    "server": env("OTA_SERVER", "https://192.168.0.100:8443"),
    "token": env("OTA_TOKEN", "bench-admin-token-1"),
    "ca_node": env("OTA_CA_NODE", HOME + "/bench-ca.pem"),
    # THE CA LIVES IN THE ROMFS, not on /flash. A FAT filesystem is corruptible -- a cancelled
    # run has wedged the mimxrt's /flash before -- and anything OTA REQUIRES must survive that.
    # Baked into app/ it ships inside every image the harness builds (golden and update alike),
    # so it is present on any slot the board can actually boot, and an OTA replaces it in step.
    # Measured cost of the old way: after a deliberate watchdog bite the file was simply gone
    # and run() died 161 times on ENOENT, unable to check in at all.
    "ca_board": env("OTA_CA_BOARD", "/rom/bench-ca.pem"),
    "wifi_ssid": env("WIFI_SSID", ""),
    "wifi_pass": env("WIFI_PASSWORD", ""),
    "project": env("PROJECT_DIR", HOME + "/proj"),
    "venv": env("OTA_VENV", HOME + "/ota-venv"),
    "sdk": env("SDK_HOME", HOME + "/openmv-sdk-1.6.0"),
    "jlink": env("JLINK", HOME + "/jlink/JLinkExe"),
    "dfu": env("DFU_UTIL", HOME + "/openmv-sdk-1.6.0/bin/dfu-util"),
    "acm": env("BOARD_ACM", "/dev/ttyACM0"),
    "uart": env("BOARD_UART", "/dev/ttyUSB0"),
    # the update server's local artifact store, for tamper scenarios (corrupt/bad_sig) --
    # only reachable when the harness runs ON the server node (co-located store).
    "artifacts": env("OTA_ARTIFACTS", HOME + "/otasrv/artifacts"),
}

# The coprocessor-sync scenarios (coproc/coproc_skip, AE3-only) dirty + rewrite the coprocessor MRAM
# partition, and that write currently crashes/wedges the AE3 off USB. Keep them OUT of the default
# regression so the AE3 can run the rest of the suite; flip HIL_COPROC=1 to opt back in (for a manual
# coproc run) once the MRAM write is fixed. See regression_scenarios().
COPROC_ENABLED = env("HIL_COPROC", "") == "1"

# Boards whose ARMED-WATCHDOG leg is known-broken, kept out of the default regression so a leg we
# already know fails can't fail every PR. The H7 Plus (OPENMV4P) is the one; its other 8 scenarios
# (delta, full, rollback, corrupt, corrupt_sha, bad_sig, bad_key, bad_version) all PASS.
#
# WHAT IS ACTUALLY MEASURED (several earlier explanations here were wrong; these are the numbers):
#   * The WWDG is real and armable. `machine.WDT("WWDG", 100)` succeeds on the H743, and on the REAL
#     path (frozen module, rebuilt factory image, armed at boot by the app) the device reports
#     `_wdt is not None = True`. It is not a missing peripheral and not the 0x7f window.
#   * With it armed the board RESET-LOOPS. Counting arms per run (each reboot re-arms):
#         relax() hard=True  -> 43 reboots / 1654 feed-loop iterations  (~0.8 s per boot)
#         relax() hard=False -> 20 reboots / 5870 feed-loop iterations  (~5.9 s per boot)
#     So the hard-IRQ feed makes survival ~8x WORSE, and removing it does not fix the loop. ~5.9 s
#     is about one 5 s poll interval, which points at the check-in as the op that outruns the window.
#   * The harness reports this as "CDC missing" / "golden did not mount a valid romfs" because the
#     reset loop keeps the port from settling -- misleading, and NOT a flash failure.
#
# WORKING THEORY: the WINC1500 driver interacts badly with the feed timer / blocking socket calls.
# The H7 Plus is the only WINC board, so the next step is the Portenta and Nicla -- same STM32H7
# family and the same WWDG, but cyw4343 wifi instead. If the watchdog passes there, the WINC is
# confirmed as the differentiator; if it fails there too, the problem is H7-wide.
#
# Still runnable by hand: workflow_dispatch with scenario=watchdog. Drop the board from this set
# once the armed leg passes.
# RETRYING ALL THREE (2026-08-04). The evidence above -- ~5.9 s per boot, about one 5 s poll
# interval -- pointed at the CHECK-IN as the op that outruns the window. That check-in runs
# under _wdt_relax(), and relax() used to feed only AFTER allocating its timer: `import
# machine` + machine.Timer(), with an allocation able to trigger a collect (221-243 ms
# measured on an RT1060), all of it unfed because relax() is called from synchronous code
# where the app's own feed loop is not running. That is the same defect that reset the
# RT1060 mid-erase, and it is now fixed in openmv_wdt. So the premise these three were
# parked on no longer holds -- measure again rather than keep assuming.
#
# Also relevant: WWDG is IN RANGE on this family. The patched driver's own formula gives
# 64 * 4096 * 128 / (120e6/1000) ~= 280 ms max on the H743/H747, well above TIMEOUT_MS=100,
# so "the H7 window is too short" was never about the peripheral.
# ANSWERED 2026-08-05, and the answer is the WINC. Same STM32H7, same WWDG, same relax() fix:
#   Nicla    watchdog PASS (291 s)          cyw43
#   Portenta watchdog PASS (1117 s / 681 s) cyw43
#   H7 Plus  watchdog FAIL (1145 s)         ATWINC1500  <- the only difference
# So it is NOT the H7 family, NOT the WWDG window, and NOT the relax() defect (that one is
# fixed and is what unblocked the other two). On the H7 Plus the armed leg bites during the
# install (reset_cause=3), resets again (cause=2), and then the WINC is WEDGED -- 39
# consecutive `run: cycle failed OSError(22,)` (EINVAL) on the check-in, preceded by one
# MBEDTLS_ERR_SSL_INVALID_MAC and one TypeError. It never recovers, so no install ever runs.
# That is a WINC driver/socket-state problem, not a watchdog-window problem, and it needs
# its own investigation. The board stays out until then -- on MEASUREMENT this time, with the
# control experiment finally done.
WATCHDOG_BROKEN = {"OPENMV4P"}
# The Arduino boards are here by DECISION, not measurement: their armed-watchdog leg has never been
# run, and chasing it was explicitly deferred so the OTA legs could land. That leaves the H7 Plus
# question (WINC or H7-wide?) open -- see above. Run it by hand when you want the answer:
#   workflow_dispatch board=ARDUINO_PORTENTA_H7 scenario=watchdog

# Per-board: which side-channel UART carries markers, how it reaches the network, and
# how the golden image is flashed. Kept data-driven so a new board is one entry.
BOARDS = {
    "OPENMV_N6": {
        "cov_uart": 3,                       # UART(3) on P4/P5
        "cov_write": "install.xip",          # this board's write path (block-dev boards differ)
        "network": "lan",
        "flash": "dfu_cli",                  # golden flash via `openmv-ota flash factory` (dfu -w)
        "jlink_device": "STM32N657L0",       # debug-only name, used ONLY by _ensure_cdc to SWD-reset
    },
    "OPENMV_AE3": {
        "cov_uart": 1,                       # UART(1) on P4/P5
        "cov_write": "install.xip",
        "network": "wifi",
        "flash": "dfu_cli",                  # golden flash via `openmv-ota flash factory` (dfu -w)
        "jlink_device": "AE302F80F55D5_HP",  # debug-only device name, used ONLY to SWD-reset
                                             # the board out of a stuck DFU state (never to flash)
    },
    "OPENMV_RT1060": {
        "cov_uart": 1,                       # UART(1) on P4/P5
        "cov_write": "install.blockdev",     # mimxrt: the block-device write model, not XIP
        "network": "lan",
        "flash": "blhost_imx",
        # A J-Link is wired to the RT's SWD (debug-only device name, used ONLY to SWD-reset the board
        # back to life when it wedges off USB -- NOT for flashing; that's blhost). Recovers the case
        # where a scenario leaves the core halted/wedged with no CDC, which otherwise fails every
        # later mpremote with "failed to access /dev/ttyACM0" (the CDC flapping/gone).
        "jlink_device": "MIMXRT1062xxx6A",
        # Flash addresses + blhost/DFU device ids all live in the CLI's boards.json now -- the
        # golden flash, the /flash self-heal, and the no_slot brick all go through `openmv-ota
        # flash ...`, so the harness no longer carries a parallel copy of the flash map.
    },
    "OPENMV4P": {                            # OpenMV H7 Plus (STM32H743, QSPI ROMFS dual-slot)
        "cov_uart": 3,                       # USART3 on P4/P5 (P4=PB10 TX, P5=PB11 RX)
        "cov_write": "install.xip",          # stm32 XIP write path (the OTA dual-slot lives in QSPI ROMFS)
        "network": "wifi",                   # reaches wifi via the ATWINC1500 shield -- see bench_main_py
        "wifi_driver": "winc",               # network.WINC() + key=/security= connect (not WLAN); pins
                                             # P0-P3/P6-P8, so the P4/P5 marker UART is conflict-free
        "flash": "dfu_cli",                  # OpenMV DFU (machine.bootloader() -> 37c5:924a) + `dfu-util -w`,
                                             # via the SAME `openmv-ota flash factory` CLI as the N6
        "jlink_device": "STM32H743VI",       # debug-only name, used ONLY by _ensure_cdc to SWD-reset
    },
    # --- Arduino MCUboot boards ------------------------------------------------------------------
    # Both are STM32H7 like the H7 Plus, but with ONBOARD CYW4343 wifi instead of a WINC1500 shield.
    # That makes them the control for the H7 Plus's armed-watchdog reset loop (see WATCHDOG_BROKEN):
    # same family, same WWDG, different network driver. A passing watchdog leg here isolates the
    # failure to the WINC; a failing one makes it H7-wide.
    "ARDUINO_NICLA_VISION": {
        # The update server NEVER writes a device record for these: they sit in its
        # `unverified_boards` set (swd-ids does not register Arduino boards), so
        # registration is bypassed and OTA is served read-only -- zero-footprint by
        # design. run_cycle therefore cannot use the server record to decide when the
        # scenario is done, and scores the UART markers instead.
        "server_record": False,                # Arduino Nicla Vision (STM32H747, QSPI ROMFS dual-slot)
        "cov_uart": 4,                       # UART4 on the SDA/SCL header (J2-1=PB9 TX, J2-2=PB8 RX),
                                             # NOT the P4/P5 pads (those are SWCLK/NRST on the Nicla).
                                             # VERIFIED on the bench: driving UART(4) from the REPL
                                             # lands "MARK_UART_4" byte-perfect on the node's CP2102
                                             # (UART1 exists too but is not wired; 2/3/6/7/8 do not
                                             # exist on this board). So a silent marker stream here
                                             # is NOT the pin -- look for a stale holder of the port.
        "cov_write": "install.xip",          # stm32 XIP write path (dual-slot lives in the QSPI ROMFS)
        "network": "wifi",                   # onboard CYW4343 -- standard network.WLAN (no shield)
        "flash": "arduino_cli",              # 1200-baud touch -> MCUboot DFU, address-based dfu-util -w
        "jlink_device": "STM32H747XI_M7",    # debug-only name (M7 runs the firmware), _ensure_cdc only
        # SWD IS WIRED AND VERIFIED on the replacement board (the previous unit's pads were not, and
        # carried jlink_swd: False). Measured: `Found SW-DP 0x6BA02477 -> Cortex-M7 r1p1 -> Cortex-M7
        # identified`, and a core reset genuinely reboots it (USB device number changes, ttyACM0 back
        # in ~2 s). JLinkExe logs "Can not attach to CPU. Trying connect under reset." first and then
        # succeeds -- normal for this part, and the place to look if SWD ever seems flaky here.
    },
    "ARDUINO_PORTENTA_H7": {
        # The update server NEVER writes a device record for these: they sit in its
        # `unverified_boards` set (swd-ids does not register Arduino boards), so
        # registration is bypassed and OTA is served read-only -- zero-footprint by
        # design. run_cycle therefore cannot use the server record to decide when the
        # scenario is done, and scores the UART markers instead.
        "server_record": False,                 # Arduino Portenta H7 (STM32H747, QSPI ROMFS dual-slot)
        "cov_uart": 1,                       # UART1 (TX=pin_A9 / RX=pin_A10) -- VERIFIED end-to-end on
                                             # the bench: board writes land byte-perfect on the node's
                                             # CP2102. NB UART1 is also MICROPY_HW_UART_REPL, which is
                                             # fine while the REPL is on USB, but watch for contention.
        "cov_write": "install.xip",          # stm32 XIP write path (dual-slot lives in the QSPI ROMFS)
        "network": "wifi",                   # PRIMARY: onboard CYW4343. Ethernet is wired too and
                                             # runs as the secondary leg (delta only) -- validated on
                                             # the bench: network.LAN() up in 5 s, DHCP 192.168.0.38.
        "flash": "arduino_cli",              # same MCUboot DFU path as the Nicla
        "jlink_device": "STM32H747XI_M7",    # debug-only name (M7 runs the firmware), _ensure_cdc only
    },
    # --- Classic boards (single-image mode, file transport) --------------------------------------
    # These builds carry no TLS stack (the F427 literally has no `ssl` module -- SD/file IS the
    # M4's update path by design) and no marker UART is wired on their nodes, so their legs run a
    # DIFFERENT cycle: stage the signed artifacts on the SD card over USB-CDC, run
    # install("/sdcard/<board>-manifest.bin"), and score the CDC probe + the install's own log
    # lines instead of the server record + UART markers. See run_file_scenario. No jlink_device:
    # nothing is wired to their SWD, and the file cycle never needs a reset it can't ask for.
    "OPENMV2": {                             # OpenMV M4 (STM32F427: 39 KB heap, frozen installer)
        "cov_write": "install.xip",
        "network": "file",
        "flash": "dfu_cli",                  # same `openmv-ota flash factory` DFU path as the N6
    },
    "OPENMV3": {                             # OpenMV M7 (STM32F765: 47 KB heap, frozen installer)
        "cov_write": "install.xip",
        "network": "file",
        "flash": "dfu_cli",
    },
    "OPENMV4": {                             # OpenMV H7 (STM32H743: control_stride 32 -- the 32-byte
        "cov_write": "install.xip",          # ECC-word board the stride exists for)
        "network": "file",
        "flash": "dfu_cli",
    },
}


def ota(name):
    return CFG["venv"] + "/bin/" + name


def _human(n):
    if n is None:
        return "-"
    if n < 1024:
        return "%d B" % n
    if n < 1024 * 1024:
        return "%.1f KiB" % (n / 1024)
    return "%.2f MiB" % (n / (1024 * 1024))


def artifact_sizes(board):
    """Stat the just-published OTA artifacts -> the bytes ACTUALLY sent over the wire. Feeds the
    per-run OTA-efficiency report: the delta-vs-full saving is the delta OTA's headline win, so
    posting it on every real-hardware run makes bandwidth efficiency a TRACKED number, not a claim."""
    b = CFG["project"] + "/build"
    out = {}
    for key, fn in (("manifest", "%s-manifest.bin" % board),      # signed metadata (per install)
                    ("full_img_gz", "%s-ota.img.gz" % board),     # full-image download
                    ("delta_gz", "%s-ota.delta.gz" % board),      # delta download (the efficient path)
                    ("payload", "%s-ota.img" % board)):           # uncompressed image (-> flash)
        p = os.path.join(b, fn)
        if os.path.exists(p):
            out[key] = os.path.getsize(p)
    return out


# How long the CDC (machine.bootloader) flash route gets before we stop waiting and use the
# reset-catch instead. A real flash is ~1-2 minutes on these boards; the old 1500s was not a
# budget but an absence of one, and a reset-looping board spent all of it.
CDC_FLASH_TIMEOUT = 420

# The device's install-retry budget (installer.py: `getattr(cfg, "INSTALL_RETRIES", 3)`, and no
# project here overrides it). The reinstall scenario's second phase exhausts this budget on
# purpose, and each attempt writes a full slot, so it is the multiplier on that phase's window.
_RETRIES = 3


def sh(cmd, timeout=180, check=True, quiet=False):
    """Run a command, returning (rc, stdout+stderr). Never raises on non-zero OR timeout unless
    check -- a timeout degrades to (124, partial-output) so a check=False caller (e.g. a liveness
    probe or a J-Link reset against a hung board) can handle it, not crash on TimeoutExpired."""
    if not quiet:
        log("$ " + (cmd if isinstance(cmd, str) else " ".join(cmd)))
    try:
        p = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + (e.stderr or "") if isinstance(e.stdout, str) else ""
        if check:
            raise RuntimeError("command timed out (%ds): %s" % (timeout, cmd))
        return 124, out
    out = (p.stdout or "") + (p.stderr or "")
    if check and p.returncode != 0:
        raise RuntimeError("command failed (%d): %s\n%s" % (p.returncode, cmd, out[-2000:]))
    return p.returncode, out


def _mpremote(args, timeout=60, check=True):
    """`mpremote connect <acm> <args...>`, retrying the transient port race: right after a
    board reset/enumeration, ModemManager (or a lingering handle from the previous step) can
    hold the freshly-appeared CDC for ~a second -> "failed to access /dev/ttyACM0 (it may be
    in use by another program)". That is not a device failure -- a short backoff lets the
    holder release. A non-transient error stops immediately."""
    last = ""
    for _ in range(5):
        rc, out = sh([ota("mpremote"), "connect", CFG["acm"]] + list(args),
                     timeout=timeout, quiet=True, check=False)
        if rc == 0:
            return rc, out
        last = out
        if ("in use" in out) or ("failed to access" in out) or ("could not enter" in out):
            log("  (mpremote: port busy, retrying in 2s)")
            time.sleep(2)
            continue
        break                                    # a real error (not the port race) -> stop
    if check:
        raise RuntimeError("mpremote failed after retries (%s):\n%s"
                           % (" ".join(str(a) for a in args), last[-1500:]))
    return 1, last


def device_exec(code, timeout=60, check=True):
    """Run MicroPython on the board over the USB-CDC (mpremote). Opening the port
    DTR-resets the board, so this is only for setup/verify, never for observing a trial."""
    return _mpremote(["exec", code], timeout=timeout, check=check)


def log(msg):
    print("[hil] " + msg, flush=True)


# The coverage checklist: an OTA code path is "covered" when its (stable) log line shows
# up on the UART. Keyed on a substring so the timestamp/level prefix and args don't matter.
# These are the SAME lines the device logs normally -- the bench just captures them at DEBUG.
# Update this when a path's log wording changes (the one coupling we accept for a single,
# no-special-markers logging channel).
COVERAGE = {
    # v2 HAS NO GOLDEN SLOT, so these no longer name one. boot.py mounts whichever slot is
    # newest and valid (A or B, by install counter), and the interesting distinction is not
    # WHICH slot ran but WHETHER a slot had to be rejected to get there:
    #
    #   boot.mount     -- a slot mounted, full stop. Every healthy boot.
    #   boot.fallback  -- the newest slot was REJECTED and an older one ran instead. This is
    #                     the negative paths' signature, and a happy boot never emits it. It
    #                     replaces both boot.mount.back and boot.front_reject, which were two
    #                     ids keyed on one log line back when "the older slot" meant "golden".
    "boot: mounted": "boot.mount",
    "boot: rejected": "boot.fallback",
    "boot: no bootable slot": "boot.no_slot",
    "status: slot read": "status.slots",          # per-slot A/B report, built every check-in
    "status: slots ready": "status.slots",
    "install: slot surveyed": "install.survey",   # per-slot pre-erase read (counter/floor/version)
    "install: erasing": "install.start",          # "install: erasing <slot> (N bytes)"
    "install: write path block-device": "install.blockdev",
    "install: write path XIP": "install.xip",
    "install: representation delta": "install.delta",
    "install: representation full": "install.full",
    "install: attempt": "install.retry",
    "install: installed + armed": "install.armed",
    "install: FAILED after": "install.fallback",
    # NB: a dropped download that RESUMES logs "install: resuming at <offset>" -- deliberately NOT a
    # coverage marker. It only fires when the link actually drops mid-transfer, which no scenario can
    # make happen on demand (it is guaranteed on the H7 Plus, whose WINC aborts every transfer at
    # ~50 s, and never happens on the N6/RT), so it is a field diagnostic, not a witnessed path.
    "install: rejected before erase": "install.reject",
    # NB: a TRANSIENT pre-erase transport failure logs "install: deferred, transport error" instead of
    # the line above, so it does NOT emit install.reject (the happy-path scenarios forbid that). It is
    # deliberately NOT a coverage marker: it fires only when a link flakes (non-deterministic), so it
    # can't be a scenario's expected path -- it's a field diagnostic, not a witnessed path.
    "install: reject bad signature": "install.reject_sig",   # sig verify failed (the trust boundary)
    "install: reject untrusted key": "install.reject_key",   # key_id not in the trusted allowlist
    "install: reject vetting": "install.reject_vet",         # anti-rollback/board/platform rejected
    "install: reject sha": "install.reject_sha",             # post-erase integrity gate: image sha256 mismatch
    "install: TLS up": "install.tls",                    # a verified TLS socket to the server
    "install: fetched body": "install.fetched",          # a 2xx download body (manifest/image)
    "install: manifest accepted": "install.manifest_ok",  # sig + board/version/platform vetting passed
    "install: staged installer": "install.staged",       # installer exec'd into RAM, about to run
    # Installer write-path primitives. The block-device (mimxrt) and XIP (stm32/alif) branches
    # each log a variant of the same step; both map to ONE semantic id, so a scenario expects
    # the step (board-agnostic) and whichever branch runs satisfies it.
    "install: fetching manifest": "install.fetch_manifest",  # pre-erase manifest GET starting
    "install: downloading": "install.download",              # image download opened post-erase
    "install: writing": "install.writing",                   # "install: writing <slot>"
    "install: write path ready block-device": "write.ready",  # closures bound (def lines witnessed)
    "install: write path ready XIP": "write.ready",
    "install: erased slot block-device": "write.erased",      # target slot erased before the write
    "install: erased slot XIP": "write.erased",
    "install: erasing block block-device": "write.erased",    # in-loop erase op (loop body witness)
    "install: erasing block XIP": "write.erased",
    "install: blank probe XIP": "write.erased",
    "install: skipping blank XIP": "write.erased",
    "install: blank probe block-device": "write.erased",
    "install: skipping blank block-device": "write.erased",
    "install: back reading block-device": "write.backread",   # in-loop BACK read (delta loop body)
    "install: wrote block block-device": "write.wrote",       # a data block written to FRONT
    "install: wrote block XIP": "write.wrote",
    "install: readback block-device": "write.readback",       # written data read back for verify
    "install: readback XIP": "write.readback",
    "install: back read block-device": "write.backread",      # golden BACK read (delta reconstruct)
    "install: back read XIP": "write.backread",
    "install: complete block-device": "write.complete",       # write committed (flush / no-op)
    "install: complete XIP": "write.complete",
    "install: committed slot": "install.committed",          # commit point passed, arming next
    "install: retry cleanup": "install.retry_cleanup",        # socket closed before a download retry
    "install: rebooting": "install.reboot",                   # _reset() drained the log, about to reset
    "verify: write block-device": "verify.write",             # confirm/rollback write+readback
    "verify: write XIP": "verify.write",
    "write: rom ioctl": "verify.write",                       # the XIP rom_ioctl write primitive ran (stm32/alif)
    # Coprocessor resource sync() -- applies the embedded coproc romfs to the helper-core
    # partition (AE3 index 1) on boot, idempotent. Only fires on a coprocessor board.
    "partition: compare": "partition.compare",                # idempotence stream-compare ran
    "partition: prepared": "partition.prepare",               # WRITE_PREPARE (NOR erase / MRAM no-op)
    "partition: writing": "partition.write",                  # chunked program of the partition
    "sync: applying": "sync.applying",                        # a resource differs -> applying it
    "sync: applied resource(s)": "sync.applied",              # sync wrote >=1 resource
    "sync: already applied": "sync.skip",                     # idempotent skip (partition matches)
    "boot: ready, running app": "boot.ready",                 # boot.py finished, handing off to app
    "boot: marked slot block-device": "boot.marked",          # trial slot marked TRIED (pre-run)
    "boot: marked slot XIP": "boot.marked",
    "boot: slot marker verified": "boot.marked_verify",       # marker read back + verified
    "boot: slot read": "boot.read",                           # the XIP slot-read closure ran
    "boot: slot mounting": "boot.mount_call",                 # the mount closure ran (vfs.mount)
    "boot: no prior mount": "boot.no_prior_mount",            # mp_init didn't auto-mount /rom (blank romfs)
    "checkin: server ok": "run.checkin_http",            # the check-in POST got a 200
    "checkin: body read": "run.body_read",               # response body read to EOF (capped)
    "checkin: body chunk": "run.body_chunk",             # a bounded body chunk accumulated (read loop body)
    "checkin: content length": "run.checkin_clen",       # the server declared Content-Length -> the body
    #                                                      read is EXACT (no slow-EOF stall on the WINC)
    "checkin: parsed": "run.checkin_parsed",             # headers skipped + JSON parsed
    "checkin: closed": "run.checkin_closed",             # the check-in connection was closed (finally)
    "asset: read": "run.asset_read",                     # a shipped asset (installer.py/ca.pem) read
    "asset: closed": "run.asset_closed",                 # the shipped-asset file was closed (finally)
    "ca: from path": "run.ca_path",                      # TLS anchors resolved from a path (run())
    "ca: bytes": "run.ca_bytes",                         # TLS anchors passed as bytes (run() -> install())
    "read: slot alias": "run.read_at",                   # a slot region aliased for reading (XIP)
    "status: read": "run.status",                        # boot-result + trial markers read
    "status: boot result": "run.boot_result",            # boot.py's mirrored result tuple built
    "identity: ready": "run.identity",                   # device_id + system.json read
    "identity: device id": "run.identity_uid",           # machine.unique_id() read into identity
    "data: path": "run.data_path",                       # sync() located a bundled data/ resource
    "wdt: armed": "wdt.armed",                            # a watchdog is REALLY running (with its window)
    "wdt: feed": "run.wdt_feed",                          # watchdog fed each poll (no-op when off)
    "app: wdt STOP feeding": "wdt.stop",                  # bite test: the app deliberately stopped feeding
    "app: wdt BIT": "wdt.bit",                            # bite test: WWDG reset (reset_cause==3), then recovered
    "run: poll wait": "run.poll_tail",                   # run() loop tail reached (post-checkin)
    "clock: resolved": "run.clock",                      # NTP/RTC resolve each poll
    "clock: syncing": "run.clock",                       # openmv_rtc: untrusted clock -> one NTP sync
    "clock: ntp synced": "run.clock",                    # openmv_rtc: NTP query set the RTC
    "clock: rtc trusted": "run.clock",                   # openmv_rtc: fast path, clock already good
    "log: configured": "log.configured",                 # openmv_log: handler/UART attached (bootstrap witness)
    "confirm: floor advanced": "confirm.floor",          # anti-rollback floor raised on confirm
    "checkin: response received": "run.checkin",
    "checkin: update offered": "run.offer",
    "confirm: kept the running image": "confirm.promoted",
}


# ---------------------------------------------------------------------------
# The scenario catalog. Each entry drives the conditions that make its code paths run and
# declares what it MUST cover ("expect") and what it must NOT ("forbid"), plus how it ends:
#   end="promoted" -- device installs, trials, confirms, and promotes to the target version
#                     (the happy paths). WHICH slot it lands in is not asserted: under A/B an
#                     update goes to whichever slot was not running, so it alternates.
#   end="golden"   -- device stays on / falls back to the version it was provisioned with (the
#                     negative paths): the update is refused pre-erase, or installs then rolls
#                     back. The name is v1's; what it asserts is "the version did not move",
#                     which is still exactly right. There is no golden SLOT any more.
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
        "expect": ["boot.mount", "boot.ready", "log.configured", "run.clock", "run.checkin_http",
                   "run.body_read", "run.body_chunk", "run.checkin_clen", "run.checkin_parsed",
                   "run.checkin_closed",
                   "run.checkin", "run.status", "run.boot_result", "run.identity",
                   "run.identity_uid", "run.offer", "run.asset_read", "run.asset_closed",
                   "run.ca_path", "run.ca_bytes", "run.read_at", "run.data_path",
                   "install.fetch_manifest",
                   "install.tls", "install.fetched", "install.manifest_ok", "install.staged",
                   "status.slots", "install.survey", "install.start", "{cov_write}",
                   "install.download",
                   "install.delta",
                   "install.writing", "write.ready", "write.erased", "write.wrote",
                   "write.readback", "write.backread", "write.complete", "install.committed",
                   "install.armed", "install.reboot", "boot.marked", "boot.marked_verify",
                   "verify.write", "confirm.floor", "confirm.promoted", "boot.mount_call",
                   "boot.read"],
        "forbid": ["install.full", "install.fallback", "install.reject", "boot.fallback"],
    },
    "full": {
        "desc": "full (non-delta) image install -> trial -> confirm -> promote",
        "publish": "full", "app": "confirm", "end": "promoted",
        "expect": ["boot.mount", "run.offer", "install.start",
                   "{cov_write}", "install.full", "install.armed", "confirm.promoted"],
        "forbid": ["install.delta", "install.fallback", "install.reject"],
    },
    "corrupt": {
        "desc": "tampered image fails integrity -> retries exhausted -> falls back",
        "publish": "corrupt", "app": "confirm", "end": "golden",
        "expect": ["install.start", "install.retry", "install.retry_cleanup", "install.fallback",
                   "install.reboot", "boot.fallback"],
        "forbid": ["install.armed", "confirm.promoted"],
    },
    # THE ONLY SCENARIO THAT STARTS FROM A PROMOTED BOARD, and under v2 it is the one that
    # demonstrates why v2 exists. In v1 the device ran the promoted image with only the FACTORY
    # image behind it, so a failed second install did not cost you the update -- it cost you
    # every update ever taken. Under A/B the slot behind it holds the LAST RELEASE THAT WORKED,
    # so the same failure costs one release. Same scenario, materially different blast radius;
    # it is worth keeping precisely because it measures that.
    "reinstall": {
        "desc": "delta install -> promote, THEN a second update fails its sha256",
        "publish": "delta", "app": "confirm", "end": "promoted",
        "expect": ["boot.mount", "run.offer", "install.start", "install.armed",
                   "confirm.promoted"],
        "forbid": ["install.fallback", "install.reject"],
        "then": {
            "desc": "a SECOND install, from the PROMOTED image, fails sha256 -> keeps 1.1.0",
            "publish": "corrupt_sha", "version": "1.2.0", "end": "golden",
            # install.reject_sha is the point: the integrity gate fires on a board that is NOT
            # on its provisioned image. Marker-scored, so no end-version is asserted -- and under
            # A/B the honest expectation CHANGED: the device keeps the promoted 1.1.0 rather than
            # losing it, because the slot behind it is that release and not the factory build.
            "expect": ["install.start", "install.reject_sha", "install.retry"],
            "forbid": ["confirm.promoted"],
            # Marker-scored: see the note at the phase-2 call site. The retry-exhaust fallback to
            # golden is `corrupt`'s job and is slot-sized; asserting it here would make this
            # scenario pass or fail on how big the board's slot is rather than on the gate.
            "by_marker": True,
        },
    },
    "corrupt_sha": {
        # Distinct from `corrupt`: `corrupt` flips a COMPRESSED byte so the download fails mid-
        # decompress (a bare deflate error, before the integrity check). `corrupt_sha` flips a
        # DECOMPRESSED byte + re-gzips, so the image decompresses cleanly and the failure is the
        # sha256 gate itself (install.reject_sha) -- the actual integrity trust boundary, on HW.
        # The sha256 GATE (install.reject_sha) is the point. The retry-exhaust -> golden fallback is
        # `corrupt`'s job and is SLOW here: each attempt writes the FULL image before the sha check
        # fails (~3x the N6's 12 MiB slot -> past the watch window). So assert the gate fired and the
        # bad image was retried, never committed -- the device stays on its provisioned VERSION
        # (it never promotes 1.1.0), which satisfies end="golden".
        "desc": "image decompresses but sha256 mismatches -> integrity gate rejects the update",
        "publish": "corrupt_sha", "app": "confirm", "end": "golden",
        "expect": ["install.start", "install.reject_sha", "install.retry"],
        "forbid": ["install.armed", "install.committed", "confirm.promoted"],
    },
    "rollback": {
        # v2 gives a trial MAX_ATTEMPTS boots (default 3), not one, so this scenario now needs
        # the no_confirm app to reset that many times before the rejection lands -- each boot
        # consumes one byte of the attempt region, and only the boot after the last one rejects.
        # The app already resets on every unconfirmed trial boot, so it converges without change;
        # it just takes ~3x as long. A bench build can set [ota].max_attempts = 1 to restore v1's
        # single-shot timing, which is also the cheapest way to prove the config knob works.
        "desc": "trial never confirms -> attempts exhausted -> falls back to the previous slot",
        "publish": "delta", "app": "no_confirm", "end": "golden",
        "expect": ["install.armed", "boot.fallback"],
        "forbid": ["confirm.promoted"],
    },
    "bad_sig": {
        "desc": "manifest signature does not verify -> refused pre-erase, version unchanged",
        "publish": "bad_sig", "app": "confirm", "end": "golden",
        # run.poll_tail + run.wdt_feed live AFTER run()'s install() call: a pre-erase REJECT raises
        # out of install() (no reboot), so run() reaches the loop tail and feeds the watchdog -- the
        # happy paths reboot at install() before the tail, so this is where those two are witnessed.
        "expect": ["run.offer", "install.reject", "install.reject_sig",
                   "run.poll_tail", "run.wdt_feed"],
        "forbid": ["install.start", "install.armed", "confirm.promoted", "boot.fallback"],
    },
    "bad_key": {
        "desc": "manifest signed by a key not in the trusted allowlist -> refused pre-erase",
        # Distinct trust gate from bad_sig: bad_sig is a VALID trusted key with a broken
        # signature (crypto verify fails); bad_key is a key the device never trusted (allowlist
        # miss) -- an attacker signing with their own key. Both must reject pre-erase.
        "publish": "bad_key", "app": "confirm", "end": "golden",
        "expect": ["run.offer", "install.reject", "install.reject_key"],
        "forbid": ["install.start", "install.armed", "confirm.promoted", "boot.fallback"],
    },
    "bad_version": {
        "desc": "version <= anti-rollback floor -> device refuses pre-erase, stays golden",
        # A full image (not a delta): a delta must go golden->newer, but here the release is
        # OLDER than golden -- the device rejects it at the version check, before rep selection.
        "publish": "full", "app": "confirm", "end": "golden", "version": "0.9.0",
        "expect": ["run.offer", "install.reject", "install.reject_vet"],
        "forbid": ["install.start", "install.armed", "confirm.promoted", "boot.fallback"],
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
        # boot.no_prior_mount: the post-brick boot has both slots blank, so mp_init can't
        # auto-mount /rom -> boot.py's umount("/rom") raises -> the no-prior-mount path runs.
        "expect": ["boot.no_slot", "boot.no_prior_mount"],
        # NOT boot.mount: entering the SBL via machine.bootloader() boots the (still-valid)
        # golden ONCE before blhost erases it, so a FRONT mount precedes the brick -- expected.
        # Forbid the things that prove the device is genuinely bricked if ABSENT: it ran no app
        # (no check-in) and did no OTA.
        "forbid": ["run.checkin", "install.start", "confirm.promoted"],
    },
    "coproc": {
        "desc": "apply the bundled coprocessor romfs to the helper-core partition on boot",
        # AE3-only: it's the only board with a coprocessor partition (index 1, MRAM @0x8047E000).
        # No OTA -- the device boots golden and the app's sync() applies data/coprocessor.romfs
        # (embedded in the main image) to the partition when it differs from the bundle. The
        # golden flash never writes partition 1, so a fresh board differs -> _partition_apply
        # runs. Idempotent: a second boot would match (sync.skip) -- forbidden here so this run
        # proves the WRITE path, not just the compare.
        "publish": "none", "app": "confirm", "end": "golden",
        "expect": ["boot.mount", "boot.ready", "run.checkin", "partition.compare",
                   "sync.applying", "partition.prepare", "partition.write", "sync.applied"],
        "forbid": ["install.start", "install.armed", "sync.skip"],
    },
    "coproc_skip": {
        "desc": "coprocessor romfs already applied -> sync() is an idempotent no-op",
        # RUN AFTER coproc (partition 1 now holds the coproc romfs). sync() stream-compares,
        # finds it matches the bundle, and skips -- proving idempotence: no needless erase/write
        # of the helper-core partition on every boot.
        "publish": "none", "app": "confirm", "end": "golden",
        "expect": ["boot.mount", "run.checkin", "partition.compare", "sync.skip"],
        "forbid": ["sync.applying", "partition.prepare", "partition.write", "sync.applied"],
    },
    # --- File-transport scenarios (the classic boards' whole regression) -------------------------
    # Scored by run_file_scenario on the CDC, not by run_cycle on the server record + UART markers,
    # so expect/forbid are empty here -- the evidence is the install's own log lines in the exec
    # output plus the post-boot status probe. Only on network == "file" boards (guarded in main).
    "file_full": {
        "desc": "file transport: full image installed from the SD card -> trial -> confirm",
        "publish": "file", "app": "file", "end": "promoted", "expect": [], "forbid": [],
    },
    "file_bad_sig": {
        "desc": "file transport: tampered manifest signature -> refused pre-erase, version unchanged",
        "publish": "file", "app": "file", "end": "golden", "expect": [], "forbid": [],
    },
}

# The watchdog happy-path: the delta cycle with the deep-sleep-safe watchdog turned ON (prepare()
# flips openmv_wdt ENABLED; the 'wdt' app arms it past network bring-up + tight-feeds it). Same
# expect/forbid as delta -- an armed watchdog that outran its window would reset mid-cycle and never
# reach promoted, so reaching it IS the proof that the device services the watchdog seamlessly
# through a real OTA cycle. Runs on the OTA boards with a deep-sleep-safe WDT: N6 (WWDG, 100 ms) and
# RT1060 (WDOG) -- the RT leg additionally proves the BLOCK-DEVICE write path (readback/back_read) is
# zero-alloc enough not to trip a GC pause past the window. openmv_wdt auto-selects the WDT per port.
SCENARIOS["watchdog"] = dict(
    SCENARIOS["delta"],
    desc="watchdog ENABLED: delta install survives a real OTA cycle with the WWDG armed",
    app="wdt",
    # ...AND require proof the watchdog actually ARMED. Reaching `promoted` only implies the
    # watchdog never outran its window IF one was running at all; on a port with no machine.WDT
    # (the Alif AE3) nothing arms and the leg would pass having watched nothing. openmv_wdt logs
    # `wdt: armed <window>` the moment it has a real WDT, so require that marker too.
    expect=SCENARIOS["delta"]["expect"] + ["wdt.armed"],
)

# The watchdog NEGATIVE path: prove the WWDG actually BITES when feeding stops -- and recovers as a
# SINGLE bite. The wdt_bite app arms + feeds ~1s (steady feeding demonstrably works), STOPS (->
# wdt.stop); the window expires and the board resets; the next boot sees reset_cause()==3 and logs
# wdt.bit as it recovers into a normal feed loop. No OTA (publish="none"), so the device stays on
# golden the whole time -> end="golden". Settling ALIVE back on golden (checking in, not reset-
# looping) is itself the proof the bite was single -- a stuck-feeding app would loop forever and
# never satisfy end="golden". N6-only (WWDG=stm32); wdt.bit firing witnesses reset_cause==3 (the app
# only logs it under that cause, per section 6.3: machine.reset() clears the WWDG so boot is unwatched).
SCENARIOS["watchdog_bite"] = dict(
    publish="none", app="wdt_bite", end="golden",
    desc="watchdog ENABLED: the WWDG bites when feeding stops (reset_cause=WDT), then recovers (single bite)",
    expect=["log.configured", "boot.ready", "run.checkin", "wdt.stop", "wdt.bit"],
    forbid=["install.start", "install.armed", "confirm.promoted"],
)


def scenario_markers(board, key):
    """(expect, forbid) marker sets for a scenario, with {cov_write} resolved per board.

    boot.read (the XIP-alias slot-read closure marker) is observed ONLY on XIP/ioctl ports
    (stm32/alif): it fires reliably on OPENMV_N6 but never on the block-device OPENMV_RT1060
    across repeated runs, so it's dropped from block-device expects. Keeping it there would leave
    the promoted-early-exit's ``have`` gate permanently unsatisfied on a block-device board, so the
    harness over-runs the device into a re-offer + bad re-install + fallback -- a false failure.
    The read() lines boot.read witnesses are still covered on the XIP boards (the audit is a union)."""
    xip = BOARDS[board]["cov_write"] == "install.xip"
    def resolve(names):
        out = set()
        for n in names:
            if n == "{cov_write}":
                out.add(BOARDS[board]["cov_write"])
            elif n == "boot.read" and not xip:
                continue
            else:
                out.add(n)
        return out
    s = SCENARIOS[key]
    return resolve(s["expect"]), resolve(s["forbid"])


def regression_scenarios(board, network):
    """The scenarios a board+network leg runs in the FULL regression (scenario=all). Now that each
    run spins up its own co-located server, the tamper scenarios (corrupt/bad_sig/bad_key/
    bad_version) run on EVERY board, not just the server node. no_slot is block-device-only (a
    blhost slot-erase); coproc/coproc_skip are AE3-only (the sole coprocessor partition). A board's
    SECONDARY interface runs just delta -- proving the network path; the rest is interface-agnostic
    so there's no point re-running it on both legs."""
    if BOARDS[board]["network"] == "file":
        # The classic boards' whole regression: the happy-path file install plus one pre-erase
        # refusal (a tampered signature). The rest of the tamper set is board-agnostic device
        # logic fully covered on the N6/RT legs, and these builds cannot transport it anyway --
        # no TLS stack means no server, so every scenario that needs an offer is out of reach.
        return ["file_full", "file_bad_sig"]
    if network != BOARDS[board]["network"]:
        return ["delta"]
    # The AE3 runs a REDUCED PR suite: only its two board-SPECIFIC paths -- the happy-path delta
    # install and the armed-watchdog install (both HIL-validated). The tamper/rollback/negative paths
    # are board-agnostic device logic, fully covered on the stable N6+RT, so re-running them on the
    # AE3 adds DFU cycles (its USB/DFU is the flakier of the fleet, with a history of wedging off USB
    # on an unattended long leg) without new coverage. This keeps the AE3 off the critical path of
    # every PR while still proving its flash + watchdog on each one. coproc/coproc_skip (AE3-only) stay
    # OUT: the coprocessor-MRAM write in that path crashes/wedges the AE3 -- re-add under COPROC_ENABLED
    # once that write is fixed (they still run by hand). A normal run never touches that partition
    # (factory flash already wrote it; sync() stream-compares, matches, and skips -- no MRAM write).
    if board == "OPENMV_AE3":
        return ["delta", "watchdog"] + (["coproc", "coproc_skip"] if COPROC_ENABLED else [])
    # The Portenta runs a REDUCED suite too, for the AE3's reason: only the paths PROVEN on it.
    # Measured on the bench, running its full set for the first time -- delta, full, bad_sig and
    # bad_key pass; rollback, corrupt, corrupt_sha and bad_version do not (they time out with
    # install.* markers missing, i.e. the device never starts the install the scenario expects).
    # Those are NOT regressions: this board had only ever run `delta` before, so that is newly
    # exercised surface that does not work yet, and the negative paths are board-agnostic device
    # logic already covered on the N6 and RT.
    #
    # Landing the board on what it proves beats holding it out entirely, and beats pretending the
    # rest passes. Widen this list as the negative paths are fixed -- they still run by hand:
    #   workflow_dispatch board=ARDUINO_PORTENTA_H7 scenario=rollback
    if BOARDS[board]["flash"] == "arduino_cli":
        # The Nicla is the same STM32H747 + CYW4343 + romfs geometry as the Portenta, so it takes the
        # same list rather than being discovered from scratch. If its negative paths turn out to work
        # where the Portenta's do not, widen it -- but assume the shared silicon behaves the same
        # until the bench says otherwise.
        # `watchdog` included: same STM32H7 WWDG as the H7 Plus, cyw4343 instead of the WINC,
        # so these two are also the control that says whether the WINC was ever special.
        #
        # The negative paths are back too. They were dropped at onboarding because they timed out
        # with `install.*` markers missing -- the device never started the install -- but that was
        # BEFORE the board could reliably install anything at all: these are the boards that hit
        # the ceiling-read bug (a 256 KiB allocation to read a 68 KiB installer, fatal without
        # external SDRAM) and the marker-UART faults that made a healthy board look dead. Every one
        # of those is fixed, so the premise they were dropped on is stale. Measure again.
        return ["delta", "full", "rollback", "corrupt", "corrupt_sha",
                "bad_sig", "bad_key", "bad_version", "watchdog"]
    scs = ["delta", "full", "rollback", "corrupt", "corrupt_sha", "bad_sig", "bad_key",
           "bad_version", "reinstall"]
    # The deep-sleep-safe watchdog runs on every OTA board: the happy path (an armed WDT survives a
    # full OTA cycle -> promoted) on all of them, so every device PR proves the on-watchdog install
    # path. The negative path (the WDT actually BITES when feeding stops, then recovers as a single
    # bite) is WWDG-specific (reset_cause==3), so watchdog_bite stays N6-only -- like no_slot is
    # block-device-only.
    if board not in WATCHDOG_BROKEN:                   # see WATCHDOG_BROKEN (H7 Plus: armed WWDG
        scs.append("watchdog")                         # reset-loops off USB; its other 8 legs pass)
    if board == "OPENMV_N6":
        scs.append("watchdog_bite")
    if BOARDS[board]["flash"] == "blhost_imx":          # no_slot bricks via blhost slot-erase
        scs.append("no_slot")
    return scs


# ---------------------------------------------------------------------------
# UART marker capture -- a background reader that records every HILCOV line for the
# whole cycle, independent of the USB-CDC console and surviving every reboot.
# ---------------------------------------------------------------------------
def resolve_uart(port):
    """The marker UART's real device path. Linux assigns ``ttyUSBn`` in PLUG ORDER, so the
    configured name (BOARD_UART, default /dev/ttyUSB0) silently becomes ttyUSB1 the moment the
    bridge is re-plugged -- after which EVERY scenario fails with "could not open port" and
    ``coverage 0/N``, which reads like a dead board rather than a renamed device (it cost a full
    10-scenario RT leg exactly that way). If the configured path is gone, fall back to the node's
    USB-serial bridge -- but only when there is EXACTLY ONE, so this never silently picks the
    wrong adapter on a node that has several; ambiguity keeps the configured path and lets the
    real "no such file" error surface."""
    if os.path.exists(port):
        return port
    from serial.tools import list_ports
    found = sorted(p.device for p in list_ports.comports()
                   if p.vid is not None and "ttyUSB" in p.device)
    if len(found) != 1:
        return port           # none, or ambiguous -> don't guess; fail with the real error
    log("uart: %s is gone -- using this node's only USB-serial bridge %s (re-plug renumbering)"
        % (port, found[0]))
    return found[0]


def _uart_live_since_flash():
    """Has the board said ANYTHING on the marker UART since golden was flashed?

    "Since the flash" is the whole point. `_CAP.raw` is the capture's entire history, so it still
    holds lines the PREVIOUS firmware wrote before this flash -- and reading those as "the marker
    UART is live" is how a board that went silent at the flash passes for healthy. It cost a full
    N6 leg: the bench-file write was skipped on that stale evidence, `.hilcov_uart` was not on the
    board, so it logged to USB, no reset could be seen to "produce a boot", and the scenario failed
    28 minutes later with every device marker missing -- on a board answering its REPL the whole
    time. Same reasoning as verify_golden_uart's `fresh`."""
    return _CAP is not None and bool(_CAP.raw[_FLASH_MARK:])


_FAULT = "run: cycle failed"                 # the device's own report of a swallowed poll-cycle
#                                              exception (openmv_ota.run) -- see device_faults


def device_faults(cap):
    """Distinct ``run: cycle failed <repr>`` reports the device made, with counts, newest last.

    A board that can never install repeats ONE exception forever, and the marker set says only
    which paths did not run -- never why. Without this, the two look identical from here: no
    install markers, a timeout, and thousands of UART lines nobody reads. Summarising it names
    the fault in one line.

    Earned the hard way: a Nicla logged ``MemoryError('memory allocation failed, allocating
    262145 bytes')`` on every single poll for half an hour, and the run still reported nothing but
    a list of markers it never reached."""
    seen = {}
    for line in (cap.raw if cap is not None else []):
        at = line.find(_FAULT)
        if at >= 0:
            text = line[at + len(_FAULT):].strip()
            seen[text] = seen.get(text, 0) + 1
    return seen


_CAP = None                                  # the live UartCapture (set by start()); see _await_boot
_BOARD = None                                # the board under test (set in main); see run_cycle
_FLASH_MARK = 0                              # index into _CAP.raw at the moment golden was flashed:
#                                              everything after it is THIS golden's account of itself
#                                              (see verify_golden_uart -- "fresh" must mean "since the
#                                              flash", not "since the verify call", because the boot
#                                              being verified happens in between)


class UartCapture:
    def __init__(self, port, baud=115200):
        import serial
        dev = resolve_uart(port)
        # FREE A STALE HOLDER FIRST. A cancelled or killed run leaves its capture thread with this
        # port still open, and a second reader does NOT get a copy -- the bytes go to whichever
        # reader wins, so the new run sees a partial stream or none at all. That presents as a board
        # with no markers: the scenario waits out its whole timeout and fails, while the board is
        # sitting there logging perfectly into a port somebody else is draining. Measured on the
        # Nicla node, where a `runner` python from a cancelled run was still holding /dev/ttyUSB0.
        # (The CDC path already does this via _ensure_cdc's fuser -k; the marker UART never did.)
        rc, out = sh("fuser -k %s 2>/dev/null" % dev, check=False, quiet=True)
        if (out or "").strip():
            log("uart: freed a stale holder of %s (%s) -- a cancelled run leaks its capture"
                % (dev, (out or "").strip()))
            time.sleep(1)                    # let the killed reader actually release the fd
        self._port, self._baud = port, baud   # kept so _reopen can re-resolve after a re-enumeration
        self._ser = serial.Serial(dev, baud, timeout=0.5)
        self._ser.reset_input_buffer()
        self.markers = []                    # ordered (t, point)
        self.raw = []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def start(self, t0):
        self._t0 = t0
        # Register as THE capture so the recovery helpers can watch the board's own account of
        # itself instead of poking its USB-CDC. Probing the CDC is not free: mpremote takes the
        # REPL with a Ctrl-C, and KeyboardInterrupt is a BaseException the bench app's
        # `except Exception` never catches -- so a probe SILENTLY KILLS the running app. Measured
        # on the Portenta: probing every 3 s during boot froze it at `data: path` every time, while
        # the same board left alone reached `network up` 5.2 s later and ran the OTA loop.
        global _CAP
        _CAP = self
        self._t.start()

    def _reopen(self):
        """Re-resolve and reopen the marker UART after its port went away. Returns True on success."""
        import serial
        try:
            self._ser.close()
        except Exception:
            pass
        try:
            dev = resolve_uart(self._port)
            self._ser = serial.Serial(dev, self._baud, timeout=0.5)
        except Exception:
            return False
        log("uart: reopened %s after the port dropped" % dev)
        return True

    def _run(self):
        buf = b""
        while not self._stop.is_set():
            try:
                buf += self._ser.read(256)
            except Exception:
                # THE PORT DIED -- do not spin on it. Linux renumbers ttyUSBn on re-plug and a DFU
                # flash re-enumerates USB, so the handle opened at start-up can stop existing
                # mid-leg. `continue` alone span silently for the rest of the run: no lines, no
                # markers, and the harness then concludes the BOARD is dead. Measured on the N6 lan
                # leg -- it errored with "the board never came back ... nothing is arriving on the
                # marker UART", and the very next leg used that same board for 10/10 scenarios.
                if not self._reopen():
                    time.sleep(1)            # nothing there yet; back off instead of a hot loop
                continue
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                s = line.decode("utf-8", "replace").strip()
                if not s:
                    continue
                self.raw.append(s)
                print("[dev] " + s, flush=True)   # forward the device UART live -> the CI log, so a
                #                                   multi-minute erase/download shows progress, not silence
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

    def reset(self, t0):
        """Drop everything captured so far and restart the clock, WITHOUT closing the port.

        Lets the capture start early (so a board that dies before the CDC is usable still gets its
        boot output recorded) while the SCORED window stays exactly what it was: provisioning-phase
        markers must not count toward a scenario's expect/forbid sets."""
        self._t0 = t0
        self.markers = []
        self.raw = []

    def tail(self, n=40):
        """The last ``n`` captured lines -- what to print when a run fails, since the marker UART is
        the only channel that still works once the CDC is gone.

        ``raw`` holds lines ALREADY stripped of their newline (see _run), so join with "\\n": an
        empty join runs the whole capture together into one unreadable line, which is how the first
        version of this shipped and made the dump useless exactly when it was needed."""
        return self.raw[-n:]

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
    if net == "wifi" and BOARDS[board].get("wifi_driver") == "winc":
        # The H7 Plus (and classic H7) reach wifi through the ATWINC1500 shield: network.WINC()
        # instead of WLAN, no active() call, and connect() takes key=/security= keywords. The socket
        # layer is identical, so run() and the whole OTA path are unchanged -- only the bring-up differs.
        bring_up = (
            'wl = network.WINC()\n'
            '    if not wl.isconnected():\n'
            '        wl.connect(%r, key=%r, security=wl.WPA_PSK)\n'
            '        while not wl.isconnected():\n'
            '            await asyncio.sleep_ms(200)\n'
            '    print("BENCH up", wl.ifconfig()[0])\n' % (CFG["wifi_ssid"], CFG["wifi_pass"])
        )
    elif net == "wifi":
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
    # Starting run() is normally unconditional, but a TRIAL boot under "no_confirm" must not poll:
    # install() REBOOTS on success, so a concurrent re-install pre-empts the app's reset and lands a
    # FRESH trial. boot.py then treats it as a first try instead of rejecting an already-tried one,
    # and the scenario spins forever re-installing (12 cycles, no rejection, observed on the H7 Plus
    # -- the WINC's slower install cycle loses that race that the faster boards happen to win). The
    # GOLDEN boot still starts run() and performs the install, so install.armed and every run.*
    # marker the scenario expects are witnessed exactly as before; only the trial boot goes quiet,
    # which is what makes the rejection deterministic instead of a coin flip.
    # recover=_bring_up: after 3 consecutive failed cycles run() rebuilds the network rather than
    # retrying a stack that may be WEDGED. This is not theoretical on the bench -- the H7 Plus's
    # ATWINC1500 reaches a state where every check-in fails `OSError(22)` (EINVAL) forever, alive and
    # polling, never recovering, which is what `reinstall` was failing on. _bring_up CONSTRUCTS a new
    # network.WINC(), and that path hard-resets the chip (winc_init -> nm_bsp_reset), so re-using the
    # same bring-up the app booted with is what clears it. Wiring it here means the fleet actually
    # EXERCISES the hook the generated main.py ships with, instead of shipping it untested.
    start_run = "    asyncio.create_task(openmv_ota.run(%r, ca=%r, poll_after_s=5, recover=_bring_up))\n" % (
        CFG["server"], CFG["ca_board"])
    if app == "no_confirm":
        start_run = ("    if not openmv_ota.status().get('trial'):\n"
                     "        asyncio.create_task(openmv_ota.run(%r, ca=%r, poll_after_s=5, recover=_bring_up))\n" % (
                         CFG["server"], CFG["ca_board"]))
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
    elif app == "wdt":
        # POSITIVE watchdog test: arm the WWDG once PAST the (slow) network bring-up, then feed on a
        # tight cadence (~20 ms << the 100 ms window) while confirming the trial. Proves an ENABLED
        # watchdog SURVIVES a real OTA cycle -- the install's ranged erase feeds per block, run() feeds
        # per poll, and this loop feeds per iteration, so nothing outruns the window and it never
        # spuriously resets. `armed=True` witnesses that ENABLED took effect (start() really armed it).
        trial_policy = (
            "    import openmv_wdt\n"
            "    openmv_wdt.start()\n"
            "    _blog.info('app: wdt armed=%r' % (openmv_wdt._wdt is not None))\n"
            "    confirmed = False\n"
            "    while True:\n"
            "        openmv_wdt.feed()\n"
            "        if not confirmed:\n"
            "            confirmed = True\n"
            "            openmv_ota.confirm()\n"
            "        await asyncio.sleep_ms(20)\n"
        )
    elif app == "wdt_bite":
        # NEGATIVE watchdog test: prove the WWDG actually BITES when feeding stops. Arm + feed ~1 s
        # (steady feeding demonstrably works), then STOP -> the window expires and the board resets.
        # On the next boot reset_cause()==3 (WDT) proves it bit; recover by feeding normally so it's a
        # SINGLE bite, not a loop. (machine.reset() cleared the WWDG per §6.3, so boot ran unwatched.)
        trial_policy = (
            "    import machine, openmv_wdt, time\n"
            "    openmv_wdt.start()\n"
            "    if machine.reset_cause() == 3:\n"
            "        _blog.warning('app: wdt BIT (reset_cause=WDT); recovered, feeding')\n"
            "        while True:\n"
            "            openmv_wdt.feed()\n"
            "            await asyncio.sleep_ms(20)\n"
            "    _blog.info('app: wdt armed=%r, feeding 1s then stopping' % (openmv_wdt._wdt is not None))\n"
            "    t0 = time.ticks_ms()\n"
            "    while time.ticks_diff(time.ticks_ms(), t0) < 1000:\n"
            "        openmv_wdt.feed()\n"
            "        await asyncio.sleep_ms(20)\n"
            "    _blog.warning('app: wdt STOP feeding -- expect a WWDG bite')\n"
            "    while True:\n"
            "        await asyncio.sleep_ms(500)\n"
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
        # The bring-up is a FUNCTION so run() can call it again as its recover hook -- one
        # definition of "get this device online", used at boot and after a wedge.
        "async def _bring_up():\n"
        "    " + bring_up +
        "\n\n"
        "async def main():\n"
        "    _blog.info('app: main() started')\n"
        # NAME THE APP THE BOARD IS ACTUALLY RUNNING. Every scenario's golden is version 1.0.0, so
        # verify_golden's payload check cannot tell one scenario's bench app from another's: when a
        # golden flash silently does not take, the board keeps the PREVIOUS scenario's app and the
        # run measures the wrong thing for 15 minutes. That is the whole of the N6 `watchdog_bite`
        # flakiness -- it always follows `watchdog`, whose app differs only in whether it stops
        # feeding, so the stale app looks entirely healthy while wdt.bit/wdt.stop never arrive.
        "    _blog.info('app: scenario %s')\n" % app +
        # Print the device_id on the UART so the harness never has to take the REPL to learn it.
        # Reading it over mpremote meant a Ctrl-C, and a Ctrl-C kills this app (KeyboardInterrupt is
        # a BaseException) -- the board can simply say who it is instead.
        "    _blog.info('app: device_id %s' % openmv_ota.identity().get('device_id'))\n"
        "    openmv_ota.sync()  # apply bundled coprocessor resources early (no-op if none)\n"
        "    await _bring_up()\n"
        "    _blog.info('app: network up, starting run()')\n"
        + start_run +
        trial_policy + "\n\n"
        "try:\n"
        "    import machine as _m\n"
        "    _blog.info('app: booting %s reset_cause=%d' % (openmv_ota.status().get('version'), _m.reset_cause()))\n"
        "    asyncio.run(main())\n"
        # BaseException, not Exception: a KeyboardInterrupt is NOT an Exception, and every mpremote
        # the harness runs delivers one (Ctrl-C is how it takes the REPL). Caught only as Exception,
        # that killed the app SILENTLY -- the UART simply stopped mid-boot with no reboot and no
        # traceback, which reads like a hung board or a bad image and cost hours to tell apart from
        # one. Logging it makes the harness's own footprint visible in the board's account.
        "except BaseException as e:\n"
        "    _blog.error('app: CRASHED %r' % (e,))\n"
        "    sys.print_exception(e)\n"
        # THEN STALL. A KeyboardInterrupt tends to re-fire the moment the app restarts, so the app
        # dies -> restarts -> dies again as fast as the board can boot. Measured: ~30 copies of the
        # crash line inside a SINGLE uart line, drowning the marker stream every scenario depends on
        # and turning one stray Ctrl-C into a board that looks permanently broken. The sleep bounds
        # that to one line every few seconds, so the log stays readable and the markers survive.
        # FEED WHILE STALLING. The stall is mine, and a stall with an ARMED watchdog would itself
        # provoke a bite -- turning a stray Ctrl-C into a spurious reset and breaking the very
        # scenarios that test the watchdog. Feed on the same cadence the app does, so the pause
        # bounds the log rate without changing what the watchdog sees. No watchdog module (or none
        # armed) -> a plain sleep.
        "    import time as _t\n"
        "    try:\n"
        "        import openmv_wdt as _w\n"
        "        for _ in range(250):\n"
        "            _w.feed()\n"
        "            _t.sleep_ms(20)\n"
        "    except Exception:\n"
        "        _t.sleep(5)\n"
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
    log("prepare: refresh vendored runtime + bench app")
    # The venv already has this checkout editable-installed (ci/hil/provision.sh); no pip here.
    dev = checkout + "/src/openmv_ota/build/device"
    # The project VENDORS its own copies -- the build reads those, not the package: the
    # romfs app lib (openmv_ota/openmv_cloud) AND the frozen survival modules in device/
    # (openmv_log/openmv_wdt/openmv_rtc). Refresh both so the run tests the checkout.
    sh("cp -rf %s/openmv_ota/. %s/app/lib/openmv_ota/" % (dev, CFG["project"]))
    sh("cp -rf %s/openmv_cloud/. %s/app/lib/openmv_cloud/ 2>/dev/null || true" % (dev, CFG["project"]))
    sh("mkdir -p %s/device && cp -f %s/*.py %s/device/" % (CFG["project"], dev, CFG["project"]))
    if app in ("wdt", "wdt_bite"):
        # Turn the opt-in watchdog ON for this run: flip ENABLED in the project's frozen copy so the
        # built firmware arms its deep-sleep-safe WDT (openmv_wdt auto-selects: WWDG on stm32/N6,
        # the mimxrt WDOG on the RT1060 -- both 100 ms); the app then start()s + feeds it. The
        # positive `watchdog` scenario runs on both N6 and RT1060 (the RT leg additionally proves the
        # block-device write path is zero-alloc enough not to trip a GC pause past the window);
        # `watchdog_bite` is N6-only (it asserts reset_cause()==3 after a deliberate stop).
        wdt_py = "%s/device/openmv_wdt.py" % CFG["project"]
        sh("sed -i 's/^ENABLED = False/ENABLED = True/' " + wdt_py)
        sh("grep -q '^ENABLED = True' " + wdt_py)     # fail LOUD if the sed didn't take (else the
        log("prepare: openmv_wdt ENABLED=True (watchdog scenario)")  # watchdog would silently no-op
    open(CFG["project"] + "/app/main.py", "w").write(bench_main_py(board, network, app))
    # BAKE THE COVERAGE-UART FILE INTO THE ROMFS, so seeing the board never depends on the CDC.
    # THE HARNESS DOES NOT WRITE /flash AT ALL. Written there over the REPL it needs a working
    # CDC -- and a scenario whose app ARMS THE WATCHDOG makes that impossible: every REPL touch
    # kills the app, the feed stops, the watchdog
    # bites, and the board reboots into the same armed app. So the leg that follows `watchdog` or
    # `watchdog_bite` can never receive it, logs to USB instead, and the harness reports a dead
    # board that is in fact perfectly healthy (measured: N6 wifi failed exactly this way right
    # after a watchdog_bite leg, while the leg before it passed). Baked into golden it is simply
    # there on the first boot, with nothing to negotiate. openmv_log searches /rom too.
    open(CFG["project"] + "/app/.hilcov_uart", "w").write(str(BOARDS[board]["cov_uart"]))
    # BAKE THE CA INTO THE ROMFS TOO, for the same reason and a stronger one: run() cannot even
    # check in without it, so a /flash it happens to be missing from is an un-updatable board.
    if os.path.exists(CFG["ca_node"]):
        open(CFG["project"] + "/app/" + CFG["ca_board"].rsplit("/", 1)[1], "wb").write(
            open(CFG["ca_node"], "rb").read())
    # A prior scenario may have left the AE3 stuck in DFU (no CDC); recover BEFORE the first
    # device op below, since these run ahead of flash_golden's own _ensure_cdc.
    _ensure_cdc(board, allow_erase=True)   # pre-flash: erasing the romfs is safe, golden follows
    # NOTHING IS WRITTEN TO /flash. Both bench files are baked into the romfs above, so there is
    # no CDC write to defer, no REPL to take, and nothing to re-deliver after the golden flash.


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
    # NO POST-FLASH BENCH-FILE WRITE. Both files ride in the romfs the flash just wrote, so
    # they are on the board the moment it boots -- no CDC wait, no REPL, no reset-to-re-read.


def _partial_download(out):
    """True if a dfu-util failure looks like a download that died PARTWAY, rather than one that
    never started. The distinction matters: a write that never began leaves the old firmware
    intact, while one that stopped halfway has already corrupted it -- and only the second needs
    (or is helped by) a two-stage firmware recovery."""
    text = out or ""
    return "Download" in text and ("LIBUSB_ERROR" in text or "get_status" in text)


def _flash_dfu_cli(board, bad_romfs=False):
    """Golden flash for the DFU boards (N6, AE3) via the openmv-ota CLI's `flash factory` -- the SAME
    tooling users ship with (and the recipe the OpenMV IDE uses). It enters DFU with
    machine.bootloader() and writes every partition with `dfu-util -w` (wait-for-device): the N6 is
    firmware (alt 1) + main romfs (alt 3, --reset); the AE3 adds the HE-core firmware (alt 2) and the
    coprocessor romfs (alt 3). `-w` is the bit a hand-rolled dfu-util kept dropping -- without it
    dfu-util races the board's slow re-enumeration after machine.bootloader() and fails as 'No DFU
    capable USB device available' or a write timing out at 0 bytes (which read as a bricked board).

    Writing the AE3 coprocessor partition also makes the runtime sync() find it matching the bundle
    and SKIP -- never the coprocessor-MRAM write that wedges the AE3 (see COPROC_ENABLED). J-Link
    stays ONLY for _ensure_cdc recovery (an SWD nRST pulse revives a board wedged off USB, which the
    CLI's DFU path -- needing a live CDC to enter the bootloader -- can't reach); it never flashes."""
    # Mark where THIS golden's account of itself begins, so verify can tell a fresh mount
    # from the one it replaced (see verify_golden_uart).
    global _FLASH_MARK
    _FLASH_MARK = len(_CAP.raw) if _CAP is not None else 0
    if bad_romfs:
        raise RuntimeError("no_slot (bad_romfs) flash not implemented for %s yet" % board)
    _ensure_cdc(board, allow_erase=True)   # pre-flash: safe to erase; the flash below reprovisions
    argv = [ota("openmv-ota"), "flash", "factory", CFG["project"], "-b", board,
            "--sdk-home", CFG["sdk"], "--dfu-util", CFG["dfu"], "--mpremote", ota("mpremote")]
    if _cdc_responsive():
        # BOUNDED, AND WITH A FALLBACK. `_cdc_responsive()` answers "did the port reply once", which
        # a board RESET-LOOPING at ~1 Hz does -- it is alive for most of every second. So this route
        # gets chosen for a board that cannot hold a REPL long enough to run machine.bootloader(),
        # and at 1500s that is a 25-minute hang that fails the leg and leaves the board no better.
        # Measured on the AE3 four times: the loop is its own cause (an armed watchdog bites, the
        # board reboots into the same armed app), so waiting longer never helps.
        #
        # A real flash on these boards takes ~1-2 minutes, so bound it and fall back to the
        # reset-catch, which does not need a REPL at all: it is the same DFU window, caught by
        # pulsing nRST while dfu-util is already waiting on -w. Entering DFU cannot be done as a
        # SEQUENCE against a looping board -- the two have to overlap.
        log("flash factory -> %s (openmv-ota, DFU -w)" % board)
        rc, out = sh(argv, timeout=CDC_FLASH_TIMEOUT, check=False)
        if rc != 0:
            log("flash: %s CDC route failed (rc=%d) -- retrying through the bootloader DFU window"
                % (board, rc))
            rc, out = dfu_reset_catch(board, argv + ["--in-bootloader"], timeout=1500)
            if rc != 0 and _partial_download(out) and recover_firmware(board):
                rc, out = dfu_reset_catch(board, argv + ["--in-bootloader"], timeout=1500)
            if rc != 0:
                raise RuntimeError("flash factory failed on both routes rc=%d: %s" % (rc, out[-400:]))
    else:
        # No CDC, so machine.bootloader() cannot be used to enter DFU -- and this is exactly the
        # state _ensure_cdc leaves behind when it has to erase the romfs (a board with no bootable
        # slot never presents a usable CDC, so the erase FREES DFU but cannot restore the port).
        # Flash through the reset-catch instead: the bootloader's DFU window on every reset is the
        # one door that does not need the port we don't have. Without this the recovery is a dead
        # end -- erase frees DFU, then nothing can use it.
        log("flash factory -> %s (no CDC -- via bootloader DFU window)" % board)
        rc, out = dfu_reset_catch(board, argv + ["--in-bootloader"], timeout=1500)
        if rc != 0 and _partial_download(out):
            # A download that died PARTWAY has left the firmware invalid, and that is
            # self-perpetuating: an invalid image means the bootloader keeps handing over, crashing
            # and coming back, so the DFU window stays short and the NEXT attempt dies at the same
            # place. Measured on the N6 -- 32%, then 36%, then 32% again across three runs, each
            # attempt leaving the board worse than it found it. Break the cycle the way it is broken
            # by hand: invalidate the firmware with a tiny write so the bootloader parks in DFU,
            # write the real image, THEN retry the flash that failed.
            log("flash: %s download died partway -- firmware is now invalid; two-stage recovery"
                % board)
            if recover_firmware(board):
                rc, out = dfu_reset_catch(board, argv + ["--in-bootloader"], timeout=1500)
        if rc != 0:
            raise RuntimeError("flash factory (no-CDC path) failed rc=%d: %s" % (rc, out[-400:]))
    time.sleep(15)                           # Alif/STM32N6 take a beat to boot + re-enumerate
    _ensure_cdc(board)                       # POST-flash: allow_erase stays False -- never wipe the golden just written


def _cdc_responsive(timeout=15):
    """True if the board ANSWERS on its USB-CDC. A real liveness probe, not os.path.exists(): the
    port can exist yet be unusable (EIO / 'in use'), and it can be absent entirely."""
    rc, _ = sh([ota("mpremote"), "connect", CFG["acm"], "eval", "True"],
               timeout=timeout, check=False, quiet=True)
    return rc == 0


def _await_cdc(board, budget=150):
    """Poll for a usable CDC for up to ``budget`` seconds; True as soon as the board answers.

    A FLAT SLEEP IS THE WRONG SHAPE HERE. Measured on the Portenta: 31.7 s from a reset to
    /dev/ttyACM0 even existing (`JLinkExe r,g` -> device node), and longer to answer mpremote. The
    old flat sleep(15) after a factory flash therefore guaranteed the probe that followed would fail
    on a perfectly HEALTHY board -- and what followed the failure was a reset, which restarted the
    32 s clock. Three retries ~11 s apart never let a single boot finish: a livelock that reads as a
    dead board. (It reads that way to the harness too -- the run failed with "golden did not mount a
    valid romfs" on a board whose golden was fine and running minutes later.)

    Waiting costs nothing when the board is healthy (it returns on the first answer) and costs only
    the budget when it isn't, so the budget is set well above the measured boot rather than near it.
    """
    deadline = time.time() + budget
    while True:
        if _cdc_responsive():
            return True
        if time.time() >= deadline:
            return False
        time.sleep(3)


def jlink_reset_pulse(board, timeout=60):
    """Pulse the board's PHYSICAL nRST line via the J-Link, then connect + reset + GO.

    The pin pulse (SetRESET/ClrRESET) needs no core connect, so it reaches a HUNG core that a
    SYSRESETREQ cannot; the follow-up `connect; r; g` actually RUNS the firmware, because the pulse
    alone can leave the core halted and never re-enumerating USB (observed on the N6). Belt (pin,
    for hung) and suspenders (connect+go, for halted-but-alive). No-op for a board with no J-Link.
    """
    if "jlink_device" not in BOARDS[board]:
        return False
    _free_jlink()                         # a stale JLinkExe blocks the probe, silently
    fd, sp = tempfile.mkstemp(suffix=".jlink", prefix="recover-")
    os.write(fd, b"si SWD\nspeed 4000\nSetRESET\nSleep 250\nClrRESET\nSleep 200\nconnect\nr\ng\nqc\n")
    os.close(fd)
    try:
        # -AutoConnect 0: do NOT attach the (possibly hung) core on launch -- the pin pulse is
        # physical and must not be gated on a core connect that would hang on a wedged board.
        sh([CFG["jlink"], "-device", BOARDS[board]["jlink_device"], "-if", "SWD",
            "-speed", "4000", "-AutoConnect", "0", "-CommanderScript", sp],
           timeout=timeout, check=False, quiet=True)
    finally:
        os.unlink(sp)
    return True


_MSC_GLOBS = ("/dev/disk/by-id/usb-MicroPy_pyboard_Flash_*-part1",
              "/dev/disk/by-id/usb-*_Flash_*-part1",   # other vendor strings for the same volume
              "/dev/disk/by-id/usb-*OpenMV*-part1")


def _msc_disk(budget=75):
    """The camera's USB mass-storage volume, or None. WAITS for it, up to ``budget`` seconds.

    A camera presents its filesystem over USB-MSC as well as the REPL, so bench files can be dropped
    in as plain files -- no mpremote, no Ctrl-C, nothing that can kill a running app. Resolved
    through /dev/disk/by-id so it is the BOARD's disk and not whatever sdX enumerated first, and it
    refuses to guess when more than one camera is attached (as resolve_uart does for the UART).

    THE WAIT IS THE POINT. This disk only exists once the firmware is up, and these boards take
    ~33 s to enumerate. A single check right after a reset finds nothing, falls back to the REPL --
    and the REPL is what kills the app, which resets the board, which unenumerates the disk. That
    loop was observed: a board rebooting once a second with `app: CRASHED KeyboardInterrupt()` on
    every boot, because the one check happened while it was down.
    """
    deadline = time.time() + budget
    while True:
        for pattern in _MSC_GLOBS:
            disks = sorted(glob.glob(pattern))
            if len(disks) > 1:
                log("bench files: %d camera disks attached -- refusing to guess" % len(disks))
                return None                  # ambiguous: writing to the wrong board is worse
            if disks:
                return disks[0]
        if time.time() >= deadline:
            return None
        time.sleep(3)


def _await_boot(board, marker="boot: ready", budget=150):
    """Wait for the board to boot by WATCHING ITS UART, touching nothing. True once seen.

    Prefer this to _await_cdc wherever the app is supposed to keep running. An mpremote probe is not
    a passive question: it takes the REPL with a Ctrl-C, and KeyboardInterrupt is a BaseException
    that the bench app's `except Exception` does not catch, so the probe silently kills the app it
    was checking on. Measured on the Portenta -- probing every 3 s through the boot froze it at
    `data: path` every single time, no reboot and no further output, which reads exactly like a hung
    board or a bad image; left alone, the same board reached `app: network up` 5.2 s later and ran
    the OTA loop indefinitely.

    Falls back to the CDC probe only when there is no capture to watch (a board with no cov_uart).
    """
    if _CAP is None:
        return _await_cdc(board, budget=budget)
    seen = len(_CAP.raw)                     # only lines from HERE count, not a previous boot's
    deadline = time.time() + budget
    while time.time() < deadline:
        if any(marker in ln for ln in _CAP.raw[seen:]):
            return True
        time.sleep(2)
    return False


def _free_jlink():
    """Reap a stale JLinkExe before driving the probe. A J-Link is a SINGLE-CLIENT device: one
    leftover process (a previous invocation that hung and outlived its timeout) makes every later
    connect fail, and those failures are silent -- `sh(..., check=False)` returns, the helper reports
    success, and the board is simply never reset.

    That is why the N6's SWD reset works when watchdog_bite runs ALONE and fails after nine prior
    scenarios: the leftovers accumulate. Only one J-Link operation is ever in flight per node, so
    anything still running here is by definition stale."""
    rc, out = sh("pkill -f JLinkExe 2>/dev/null; true", check=False, quiet=True)
    del rc, out


def jlink_core_reset(board, timeout=60):
    """Reset a board through the DEBUG CORE (``connect; r; g``) -- no nRST pin, no DFU-window games.

    This is the SAFE half of jlink_reset_pulse. The pin pulse in that function is what leaves an
    Arduino board halted with no USB at all when its follow-up ``connect`` fails; a core reset has
    brought the Portenta back every single time it was tried (full boot, CDC at ~32 s, app checking
    in). Use this for the MCUboot boards; keep the pin pulse for the ones that need it to reach a
    genuinely hung core. No-op for a board with no J-Link.
    """
    if "jlink_device" not in BOARDS[board]:
        return False
    _free_jlink()                         # a stale JLinkExe blocks the probe, silently
    fd, sp = tempfile.mkstemp(suffix=".jlink", prefix="corereset-")
    if BOARDS[board].get("jlink_swd", True):
        os.write(fd, b"si SWD\nspeed 4000\nconnect\nr\ng\nqc\n")
    else:
        # No usable SWD on this board -- drive the probe's RESET PIN and nothing else.
        # SetRESET/ClrRESET toggle a J-Link output; they need no target connection, so they work
        # when SWCLK/SWDIO are not wired at all (the Nicla: `connect` returns "Could not connect to
        # the target device" every time). Without `connect` the debugger never halts the core, so
        # the board simply reboots and runs -- which is the whole point of the reset.
        os.write(fd, b"si SWD\nspeed 4000\nSetRESET\nSleep 250\nClrRESET\nSleep 200\nqc\n")
    os.close(fd)
    try:
        sh([CFG["jlink"], "-device", BOARDS[board]["jlink_device"], "-if", "SWD",
            "-speed", "4000", "-AutoConnect", "0", "-CommanderScript", sp],
           timeout=timeout, check=False, quiet=True)
    finally:
        os.unlink(sp)
    return True


def dfu_reset_catch(board, argv, *, settle=2.0, timeout=900):
    """Run a dfu-util-backed command that WAITS for a DFU device (``-w``), pulsing the board's reset
    line while it waits so the bootloader's BRIEF DFU window gets caught.

    THIS IS THE RECOVERY PRIMITIVE FOR A BOARD WHOSE CDC IS GONE. Normally the CLI enters DFU with
    ``machine.bootloader()`` over the USB-CDC -- useless when the CDC is exactly what's broken. But
    the OpenMV bootloader presents DFU for a short window on EVERY reset, so: start the command (it
    blocks on ``-w``), pulse nRST a moment later, and dfu-util catches the window. Verified on the
    H7 Plus, and it is how a board whose app owns/kills the CDC gets recovered without touching it.

    Pass the CLI's ``--in-bootloader`` in ``argv`` so it does NOT try the CDC route first.
    Returns (rc, output).

    OPENMV BOOTLOADER ONLY. The Arduino MCUboot boards have no reset-triggered DFU window, so the
    pulse just reboots the app while ``-w`` waits for a bootloader that never appears -- a silent
    hang for the whole timeout. Refused outright below rather than left as a foot-gun; those boards
    go through ``_arduino_dfu_run``.
    """
    if BOARDS[board].get("flash") == "arduino_cli":
        raise RuntimeError(
            "dfu_reset_catch is the wrong primitive for %s: Arduino MCUboot presents no DFU window "
            "on reset, so the -w wait can only hang. Use _arduino_dfu_run." % board)
    log("recover: %s -- dfu(-w) + nRST pulse to catch the bootloader's DFU window" % board)
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(settle)                       # let dfu-util reach its wait loop before we reset
    jlink_reset_pulse(board)
    try:
        out = proc.communicate(timeout=timeout)[0]
    except subprocess.TimeoutExpired:
        proc.kill()
        return 124, "dfu_reset_catch timed out"
    return proc.returncode, out or ""


def _dfu_present():
    """True iff an MCUboot DFU device is enumerated RIGHT NOW. A point-in-time check, never a wait:
    the whole class of bug this guards against is ``dfu-util -w`` blocking on a device that is not
    coming."""
    rc, out = sh([CFG["dfu"], "-l"], check=False, timeout=20, quiet=True)
    return "Found DFU" in (out or "")


def _arduino_dfu_run(board, argv, what, *, timeout):
    """Run a dfu-util-backed CLI command on an Arduino MCUboot board, entering DFU the only way
    these boards actually support. Returns (rc, output).

    MCUboot has NO reset-triggered DFU window (the OpenMV bootloader's trick -- see
    dfu_reset_catch -- has no counterpart here). Entry is exclusively the 1200-baud touch, and that
    touch is answered by the firmware's USB stack, NOT by the running app: it still works while the
    app owns the REPL and every mpremote times out. So an unresponsive CDC is NOT a reason to reach
    for nRST -- an enumerated port is all the touch needs.

      * already in DFU     -> run with --in-bootloader; the device is there NOW, so -w returns at once
      * a runtime port     -> plain run; the CLI does its own touch
      * neither            -> fail FAST (rc 125); the board needs a power cycle, and no amount of
                              waiting will conjure a bootloader

    Measured cost of getting this wrong: a `flash erase --in-bootloader` was launched behind an nRST
    pulse against a Portenta running its app, and `dfu-util -w -d ,2341:035b` sat waiting for a
    bootloader id while the board sat enumerated as 2341:045b (its runtime CDC). Eleven minutes of
    silent hang, and the job never reached the publish step -- the device just kept checking in
    against a server that had no release, which reads like an OTA bug and is not one.
    """
    if _dfu_present():
        log("%s -> %s (already in DFU -- no wait)" % (what, board))
        return sh(argv + ["--in-bootloader"], timeout=timeout, check=False)
    if os.path.exists(CFG["acm"]):
        log("%s -> %s (CLI 1200-baud touch via %s)" % (what, board, CFG["acm"]))
        return sh(argv, timeout=timeout, check=False)
    return 125, ("%s: %s has neither a DFU device nor a port at %s -- it cannot be reached without "
                 "a power cycle" % (what, board, CFG["acm"]))


def _flash_raw(board):
    """The board's flash config from the PACKAGE (boards.json), or None. Read it rather than
    duplicating usb ids and alt numbers in the harness: two copies drift, and a stale alt writes
    firmware into the wrong partition."""
    try:
        from openmv_ota.flash.targets import flash_config
        return flash_config(board).raw
    except Exception as e:                   # not installed, or an unknown board
        log("flash config for %s unavailable: %s" % (board, e))
        return None


def recover_firmware(board, firmware=None, timeout=400):
    """Reflash MAIN FIRMWARE on a board looping bootloader -> crash -> bootloader. Returns True iff
    the board came back on USB.

    THE PROBLEM: corrupt firmware does not leave a board dead, it leaves it CYCLING. The bootloader
    hands over, the firmware crashes, the bootloader comes back -- so the board is re-enumerating
    every couple of seconds. Every USB operation then dies partway through, and the errors point
    everywhere except the cause: `dfu-util` LIBUSB_ERROR_IO a third of the way into a download, an
    i.MX SBL that "did not enumerate / could not be claimed", ttyUSB renumbering, a CDC that appears
    and vanishes between probes. It reads as flaky hardware -- it is not, it is deterministic.

    THE FIX IS TWO STAGES, and the order is the whole trick:

      1. write a SECTOR OF ZEROS to the firmware alt. Tiny, so it fits inside the short window the
         cycling board offers. Now nothing valid boots, so the bootloader stops handing over and
         PARKS in DFU;
      2. write the real firmware, with the board sitting still and no time limit.

    Both stages use `-w` STARTED FIRST and then a reset pulse, so dfu-util is already waiting and
    catches the window at its start rather than landing mid-cycle. Measured on the N6: a direct
    full write died at 32% and then 36%; two-stage completed and the board came back as
    37c5:1206 with a working CDC.

    OpenMV DFU boards only -- an Arduino board's MCUboot has no reset window (see _arduino_dfu_run),
    and the imx boards flash through their SBL, not DFU.
    """
    raw = _flash_raw(board)
    if raw is None or "alt" not in raw or "firmware" not in raw.get("alt", {}):
        log("recover: %s has no DFU firmware alt -- not a DFU board" % board)
        return False
    usb, alt = raw["usb"], raw["alt"]["firmware"]
    firmware = firmware or "%s/build/%s-firmware.bin" % (CFG["project"], board)
    if not os.path.exists(firmware):
        log("recover: no firmware image at %s -- build one first" % firmware)
        return False

    zero = os.path.join(tempfile.mkdtemp(prefix="fwzero-"), "zero.bin")
    with open(zero, "wb") as fh:
        fh.write(b"\x00" * 4096)
    try:
        log("recover: %s stage 1/2 -- invalidate the firmware so the bootloader stops handing over"
            % board)
        rc, out = dfu_reset_catch(board, [CFG["dfu"], "-w", "-d", ",%s" % usb,
                                          "-a", str(alt), "-D", zero], timeout=120)
        if rc != 0:
            log("recover: stage 1 failed rc=%d -- %s" % (rc, (out or "")[-200:]))
            return False
        time.sleep(5)                        # let it settle into DFU with nothing to boot
        # CHECK that stage 1 actually parked it -- do not assume. When it did, a plain `-w` returns
        # at once and the write has all the time it needs. When it did NOT, that same `-w` waits for
        # a device that is not coming and burns the whole timeout (measured: rc=124 after 400 s),
        # so fall back to the reset-catch, which MAKES a window instead of hoping for one.
        argv = [CFG["dfu"], "-w", "-d", ",%s" % usb, "-a", str(alt), "--reset", "-D", firmware]
        if _dfu_present():
            log("recover: %s stage 2/2 -- full firmware write (parked in DFU)" % board)
            rc, out = sh(argv, timeout=timeout, check=False)
        else:
            log("recover: %s stage 2/2 -- not parked after stage 1; writing via the reset window"
                % board)
            rc, out = dfu_reset_catch(board, argv, timeout=timeout)
        if rc != 0:
            log("recover: stage 2 failed rc=%d -- %s" % (rc, (out or "")[-200:]))
            return False
    finally:
        os.unlink(zero)
    time.sleep(12)                           # let it boot and re-enumerate
    back = os.path.exists(CFG["acm"])
    log("recover: %s firmware reflashed; CDC %s" % (board, "back" if back else "STILL GONE"))
    return back


def recover_erase_romfs(board):
    """LAST-RESORT recovery: erase the board over DFU so it boots with NO app.

    An app that wedges or owns the CDC (e.g. one polling an unreachable server) makes every
    mpremote fail, which blocks the harness's own reflash -- a deadlock, since flashing needs the
    CDC to enter the bootloader. Erasing breaks it: with no bootable slot the app never runs, the
    CDC comes back, and the normal `flash factory` can reprovision. Needs no built artifacts, so it
    works even before a build. (On the Arduino boards the configured erase covers firmware AND the
    romfs partition, not the romfs alone -- the follow-up factory flash restores both.)
    """
    argv = [ota("openmv-ota"), "flash", "erase", CFG["project"], "-b", board,
            "--sdk-home", CFG["sdk"], "--dfu-util", CFG["dfu"], "--mpremote", ota("mpremote")]
    if BOARDS[board].get("flash") == "arduino_cli":
        rc, out = _arduino_dfu_run(board, argv, "recover: erase", timeout=300)
    else:
        rc, out = dfu_reset_catch(board, argv + ["--in-bootloader"])
    log("recover: romfs erase rc=%d%s" % (rc, "" if rc == 0 else " -- %s" % out[-300:]))
    return rc == 0


def _ensure_cdc(board, allow_erase=False):
    """Recover a board that has wedged off USB (no CDC) via a J-Link SWD reset, so a later scenario
    doesn't fail every mpremote with "failed to access /dev/ttyACM0". Two ways a board loses its CDC:
    the AE3's machine.bootloader() flash path is unreliable at LEAVING DFU (documented Alif USB
    re-enum flakiness) and sits in DFU with no CDC; and a mimxrt/stm32 board can be left halted or
    crashed by a scenario (SWD still alive, USB dead OR flapping/held -- the port can be enumerated
    yet unusable, failing every mpremote "in use"). Recovery is a HARDWARE nRST pulse driven straight
    on the physical reset line (SetRESET/ClrRESET toggle the J-Link's RESET pin) -- NOT a SYSRESETREQ
    through the debug core, which needs a live core connect and so hangs on a deeply wedged board. The
    pin pulse resets ALL processor state regardless of core state (validated on the bench: it recovered
    an RT already dropped off USB, and an AE3 whose core would not even attach), boots the golden
    firmware, and brings a clean CDC back -- so these boards never need a physical power cycle. Probes
    RESPONSIVENESS (not mere existence), no-ops once the board answers, and only for boards with a
    J-Link reset device (a debug-only name used ONLY to reset -- flashing stays each board's normal
    path)."""
    if "jlink_device" not in BOARDS[board]:
        return
    for attempt in range(3):
        # A real liveness probe, not just os.path.exists(): the CDC can be enumerated yet held or
        # flapping (fails every mpremote "in use"), which an existence check misses. (Opening the
        # port DTR-resets the board, but prepare reflashes golden right after, so that reset is free.)
        rc, _ = sh([ota("mpremote"), "connect", CFG["acm"], "eval", "True"],
                   timeout=15, check=False, quiet=True)
        if rc == 0:
            return
        # Say which lever is actually about to be pulled: the Arduino branch below WAITS rather than
        # resetting, and a log line claiming a reset that never happened is exactly the kind of
        # misdirection that cost hours today.
        how = ("wait for the boot" if BOARDS[board].get("flash") == "arduino_cli"
               else "J-Link SWD reset")
        log("recover: %s CDC missing/unresponsive at %s -- free holders + %s (try %d)"
            % (board, CFG["acm"], how, attempt + 1))
        sh("fuser -k %s 2>/dev/null || true" % CFG["acm"], check=False, quiet=True)  # any host holder
        # On the Arduino MCUboot boards an nRST is the WRONG tool and actively makes things worse:
        # the 1200-baud touch that entered DFU sets a "stay in bootloader" flag in RAM, and RAM
        # SURVIVES the reset pin -- so pulsing nRST lands the board back in DFU (no CDC) instead of
        # recovering it. Measured on the Portenta: after a flash the app had booted (markers on the
        # UART), then three reset pulses took the CDC away for good. Leave DFU properly instead;
        # only fall back to the pin pulse if it is not in DFU at all.
        if BOARDS[board].get("flash") == "arduino_cli":
            if _dfu_leave(board):
                continue                               # left DFU -> re-probe rather than reset it
            # NOT in DFU and not answering. Order matters here, and getting it wrong fails both ways:
            #   * reset FIRST and you restart a 33 s boot every retry, so the board never finishes
            #     one and the port is absent for the window verify needs it;
            #   * never reset at all and a board that is genuinely wedged off USB stays wedged --
            #     the whole run then fails before it flashes, with "neither a DFU device nor a port".
            # So be patient once (it may simply still be booting), then assertive. A CORE reset
            # revives this board reliably -- measured repeatedly, /dev/ttyACM0 back ~33 s later, from
            # a state with no USB device of any kind. Never the nRST PIN: that leaves the core halted
            # with no USB at all.
            if attempt == 0:
                _await_boot(board, budget=45)
            else:
                jlink_core_reset(board)
                _await_boot(board, budget=60)
            continue
        jlink_reset_pulse(board)                       # see jlink_reset_pulse for why pin THEN core
        time.sleep(8)
    # A reset alone cannot fix a board whose APP is what breaks the CDC -- it just reboots straight
    # back into it. Erasing the romfs over DFU removes the app, and is the only lever that does not
    # need the CDC we don't have. It is DELIBERATELY a one-way step: measured on the H7 Plus, a board
    # with no bootable slot does NOT come back on USB (boot.py has nothing to mount, and the port
    # never becomes usable), so do NOT probe for the CDC afterwards and do NOT treat its absence as
    # failure -- the follow-up golden flash over DFU is what restores the port.
    #
    # allow_erase GATES THIS BECAUSE IT IS DESTRUCTIVE. It is only ever right BEFORE a flash. The
    # post-flash call must never erase: it would wipe the golden that was just written, and did --
    # a run flashed golden through the DFU window, found the CDC not yet back, and erased it again.
    if not allow_erase:
        log("recover: %s CDC still gone; not erasing (a post-flash erase would destroy the image "
            "just written) -- caller must handle it" % board)
        return
    recover_erase_romfs(board)
    log("recover: %s romfs erased -- the golden flash will reprovision it over DFU "
        "(no CDC expected until then)" % board)


def _dfu_leave(board):
    """Boot a board out of its MCUboot DFU bootloader WITHOUT the J-Link -- the same "leave" the
    OpenMV IDE issues. Recovers a board stuck in DFU (an interrupted flash, or a manual bootloader
    entry) when its SWD is unavailable, which the Nicla's tiny SWD pads often are. Returns True iff
    a DFU device was present and detached. ``dfu-util -e`` (a bare detach) is a NO-OP on the Arduino
    MCUboot bootloader -- what actually boots it is a manifest-leave (``-s :leave``) plus a USB
    reset (``-R``). Only reached when the CDC is already missing, so it never disturbs a live board."""
    if not _dfu_present():
        return False                             # not in DFU -> nothing to leave; fall through
    log("recover: %s is in DFU -- dfu-util leave + reset (boots firmware, no J-Link)" % board)
    sh([CFG["dfu"], "-a", "0", "-s", ":leave", "-R"], check=False, timeout=30, quiet=True)
    time.sleep(6)                                # let it leave DFU + re-enumerate its CDC
    return True


def _flash_arduino_cli(board, bad_romfs=False):
    """Golden flash for the Arduino MCUboot boards (Nicla Vision, Portenta H7) via the openmv-ota
    CLI's `flash factory`. The arduino backend enters DFU with an automatic 1200-baud touch, then
    writes firmware + romfs (+ the CYW4343 wifi blobs) with address-based `dfu-util -w`.

    Unlike the DFU boards, the CLI's arduino factory resolves the romfs partition as
    ``<board>-romfs.img``, so stage the dual-slot factory image under that name first
    (``build factory-romfs`` emits ``<board>-factory-romfs.img``; the wifi blobs are already dropped
    into build/ by ``build firmware``). Same rename the mimxrt path does.

    DFU entry is `_arduino_dfu_run`'s business: the 1200-baud touch, or a direct write if the board
    is already in DFU. Note what does NOT work here -- the OpenMV path's "wait on -w and pulse nRST
    to catch the bootloader's window" -- because MCUboot has no such window; that fallback used to
    live here and only ever hung."""
    if bad_romfs:
        raise RuntimeError("no_slot (bad_romfs) flash not implemented for %s yet" % board)
    build = CFG["project"] + "/build"
    sh("cp -f %s/%s-factory-romfs.img %s/%s-romfs.img" % (build, board, build, board))
    # Mark where THIS golden's account of itself begins: every UART line from here on belongs to the
    # image about to be written, so verify can tell a fresh mount from the one it replaced.
    global _FLASH_MARK
    _FLASH_MARK = len(_CAP.raw) if _CAP is not None else 0
    # Reach the board WITHOUT taking its REPL. _ensure_cdc probes with mpremote, and that Ctrl-C
    # kills the running app -- which then restarts, gets interrupted again, and spins (see the
    # bench app's crash handler). The flash needs no REPL at all: only a DFU device to write to, or
    # an enumerated port to 1200-baud touch. Check for those directly, and reset only if the board
    # offers neither -- that is the genuinely wedged case a core reset revives in ~33 s.
    if not _dfu_present() and not os.path.exists(CFG["acm"]):
        log("flash: %s offers neither DFU nor a port -- core reset, then wait for it" % board)
        jlink_core_reset(board)
        _await_boot(board, budget=90)
    argv = [ota("openmv-ota"), "flash", "factory", CFG["project"], "-b", board,
            "--sdk-home", CFG["sdk"], "--dfu-util", CFG["dfu"], "--mpremote", ota("mpremote")]
    # RETRY THE DFU WRITE. dfu-util intermittently fails to read the device's descriptors right
    # after the 1200-baud touch -- "Failed to retrieve string descriptor 7 / Could not read name /
    # Failed to parse memory layout for alternate interface 1", exit 74 -- i.e. it opened the
    # device before the USB configuration was fully enumerated. It is a race in PROVISIONING, not
    # anything the scenario is testing, and it lands on whichever scenario happens to be running:
    # two consecutive Portenta legs failed this way, first on `delta`, then on `full`, with all
    # seven other scenarios passing both times. Left alone it costs a whole 9-scenario leg (~3 h)
    # to a transient. Bounded and loud, so a board that is genuinely unflashable still fails.
    for attempt in range(3):
        rc, out = _arduino_dfu_run(board, argv, "flash factory", timeout=1500)
        if rc == 0:
            break
        log("flash: %s DFU write failed (attempt %d/3)\n%s" % (board, attempt + 1, out[-300:]))
        time.sleep(10)                       # let the device finish enumerating before re-opening
        if "LIBUSB_ERROR_PIPE" in (out or ""):
            # THE DEVICE IS WEDGED, NOT RACING -- retrying alone cannot fix it. A DfuSe device that
            # hits an error enters dfuERROR and STALLS every subsequent control transfer, so each
            # attempt fails identically ("Error during special command ERASE_PAGE download: -9").
            # That is exactly what happened on the Portenta: three identical failures, and then
            # EVERY later scenario in the leg failed the same way, on both legs, because nothing
            # ever cleared the state. Confirmed by hand -- once cleared the device reports
            # "dfuIDLE, No error condition" and erases fine.
            # Reset it out of DFU through the debug core (no nRST: the stay-in-bootloader flag
            # lives in RAM and survives the pin), let it boot, and let the next attempt re-enter
            # DFU with the 1200-baud touch from a clean state.
            log("flash: %s is in dfuERROR (stalled control transfer) -- core-resetting out of DFU"
                % board)
            jlink_core_reset(board)
            _await_boot(board, budget=90)
    if rc != 0:
        raise RuntimeError("arduino flash factory failed rc=%d after 3 attempts: %s"
                           % (rc, out[-400:]))
    # Just WATCH it come back. Measured by hand, running this exact command and touching nothing:
    # USB drops at :leave and the board re-enumerates 31 s later on its own, with the app checking
    # in -- so `:leave` needs no help. (An earlier claim here that it did was an artifact of the
    # probing that used to follow: an mpremote probe Ctrl-C's the app dead, because KeyboardInterrupt
    # is a BaseException the app does not catch, and the retries then reset the board out from under
    # its own 33 s boot.) No probe, no reset: neither is needed, and both did damage.
    if not _await_boot(board):
        _dfu_leave(board)                    # stuck in DFU? leave it without needing the J-Link


def _flash_blhost_imx(board, bad_romfs=False):
    """Provision golden on the mimxrt (RT1062) via the openmv-ota CLI's resident-SBL flash path
    (`flash firmware` + `flash romfs`): the CLI enters the resident SBL with machine.bootloader()
    (no jumper) and, running post-FCB, needs no FlexSPI config -- see openmv_ota.flash.imx. This
    replaces the harness's hand-rolled blhost sequence: the everyday golden flash now goes through
    the same tooling users ship with.

    bad_romfs=True is the no_slot brick: blank the whole OTA romfs region (both slots -> no valid
    trailer -> boot.py finds nothing bootable), leaving firmware + /flash intact -- via the CLI's
    `flash erase --romfs` (which enters the resident SBL and flash-erase-regions the romfs)."""
    # Mark where THIS golden's account of itself begins, so verify can tell a fresh mount
    # from the one it replaced (see verify_golden_uart).
    global _FLASH_MARK
    _FLASH_MARK = len(_CAP.raw) if _CAP is not None else 0
    build = CFG["project"] + "/build"
    if bad_romfs:
        # KEEP THE BOARD OBSERVABLE ACROSS THE BRICK. `.hilcov_uart` is baked into the ROMFS (see
        # _bench_files: /rom is the one volume that survives an armed watchdog), and the erase below
        # destroys that whole region -- so the very act that creates the condition under test also
        # removes the file telling the device which UART to log to. openmv_log then falls back to
        # its USB branch, and boot.py's markers are printed BEFORE the CDC has enumerated, i.e. into
        # nothing. Measured on the RT1060: a healthy, enumerated, REPL-answering board that produced
        # ZERO bytes on the marker UART *and* the console for 900 s, failing on `boot.no_slot` --
        # a marker it certainly printed. (Reading the console instead does NOT work, for that same
        # enumeration reason; it only steals the port the nudge-reset needs.)
        #
        # /flash survives a romfs erase and openmv_log searches it, so seed it there first. Taking
        # the REPL is safe HERE and only here: this board's app is about to lose its filesystem.
        try:
            device_exec("f=open('/flash/.hilcov_uart','w');f.write('%d');f.close()"
                        % BOARDS[board]["cov_uart"], timeout=30)
            log("brick: seeded /flash/.hilcov_uart=%d -- /rom (which normally carries it) is about "
                "to be erased" % BOARDS[board]["cov_uart"])
        except Exception as e:                   # hil-residual: no REPL to seed through
            log("brick: could NOT seed /flash/.hilcov_uart (%r) -- the bricked boot will log to USB "
                "before the CDC enumerates, so its markers will be invisible" % (e,))
        log("brick: erase the OTA romfs region (both slots) -> openmv-ota flash erase --romfs")
        sh([ota("openmv-ota"), "flash", "erase", CFG["project"], "-b", board, "--romfs",
            "--sdk-home", CFG["sdk"], "--mpremote", ota("mpremote")], timeout=300)
        time.sleep(12)
        return
    # Golden: firmware + the factory (dual-slot) romfs. The CLI's `flash romfs` reads <board>-romfs.img,
    # so stage the factory image under that name (as the AE3 path already does), then flash both via
    # the CLI's automatable resident-SBL path -- each call does its own machine.bootloader + reset.
    sh("cp -f %s/%s-factory-romfs.img %s/%s-romfs.img" % (build, board, build, board))
    for op in ("firmware", "romfs"):
        log("flash %s -> %s (openmv-ota, resident SBL)" % (op, board))
        sh([ota("openmv-ota"), "flash", op, CFG["project"], "-b", board,
            "--sdk-home", CFG["sdk"], "--mpremote", ota("mpremote")], timeout=300)
    time.sleep(12)                                       # POR + FlexSPI re-enumerate as runtime


# A boot reports its mount in TWO forms, and both mean "a slot is up":
#   log.info("boot: mounted %s (payload %d)")                     -> "boot: mounted A"
#   log.warning("boot: rejected %s -> mounted %s (payload %d)")   -> "-> mounted B"
# Matching only the first missed every boot that got there by FALLBACK -- which is exactly what the
# negative scenarios do -- so corrupt/bad_key/bad_version all failed verify against a board that had
# booted correctly. Both forms end in "(payload N)", which is what _mounted_payload keys on, so the
# slot NAME never has to be parsed -- which is why the A/B rename did not touch this.
def _packed_version(v):
    """A dotted version as the device packs it into a trailer's payload_version -- what the boot line
    reports: `boot: mounted A (payload 16777216)` is 1.0.0, 16842752 is 1.1.0."""
    major, minor, patch = (int(x) for x in v.split("."))
    return (major << 24) | (minor << 16) | (patch << 8)


def _mounted_payload(line):
    """The payload_version out of a mount line, or None if it does not carry one."""
    if "payload " not in line:
        return None  # hil-residual: a mount line without a payload (older format)
    try:
        return int(line.split("payload ", 1)[1].split(")", 1)[0].strip())
    except ValueError:
        return None  # hil-residual: unparseable payload field


_MOUNT_MARKERS = ("boot: mounted", "-> mounted ")


def _mounted(line):
    return any(m in line for m in _MOUNT_MARKERS)


def verify_golden_uart(board, budget=300, golden="1.0.0", want_app=None):
    """verify_golden() without taking the REPL: read the board's own account off the UART.

    Same two claims as the mpremote version -- golden booted and mounted a valid romfs, and here is
    its device_id -- from `boot: mounted ...` and the bench app's `app: device_id ...` line. The
    mount claim must come from a boot AFTER this call (a stale marker would "verify" the image the
    flash replaced); the id may come from anywhere in the log, since a board's device_id does not
    change between boots.

    Why not just exec it: mpremote grabs the REPL with a Ctrl-C, which kills the running app
    (KeyboardInterrupt is a BaseException the app does not catch). On a board that takes ~33 s to
    enumerate, the retry loop around that probe reset the board out from under its own boot and
    reported "golden did not mount a valid romfs" against golden that was fine. Watching cannot do
    that -- it is the same UART the coverage markers already come from.
    """
    log("verify: golden boots + /rom mounts + device_id -- from the UART, REPL untouched")
    if _CAP is None:
        return verify_golden()               # no capture on this node: fall back to the REPL
    # "Fresh" means SINCE THE FLASH, not since this call: the boot being verified happens in
    # between (the flash's own :leave boots it, and _flash_arduino_cli waits for that). Keying off
    # this call instead would demand a SECOND boot that nothing ever triggers -- verify would sit
    # out its whole budget and fail a board whose golden had already come up perfectly.
    seen = _FLASH_MARK
    deadline = time.time() + budget
    while time.time() < deadline:
        fresh = _CAP.raw[seen:]
        mounts = [ln for ln in fresh if _mounted(ln)]
        if mounts:
            # ASSERT IT IS ACTUALLY GOLDEN. A scenario runs after the previous one may have PROMOTED
            # the device to the target, and if the golden reflash silently does not take, the board
            # simply keeps running that image. Everything downstream then measures the wrong thing:
            # observed on the N6, watchdog_bite opened its scored window with the device on
            # 1.1.0/FRONT and failed 28 minutes later on markers that could never have appeared.
            # The boot line carries the version, so this costs nothing and fails in seconds.
            payload = _mounted_payload(mounts[-1])
            want = _packed_version(golden)
            if payload is not None and payload != want:
                raise RuntimeError(
                    "%s booted payload %d, expected golden %s (%d) -- the golden flash did not take, "
                    "so this scenario would run against the wrong image. Last mount: %r"
                    % (board, payload, golden, want, mounts[-1]))
            # ASSERT THE RIGHT BENCH APP IS RUNNING. Every scenario's golden is 1.0.0, so the
            # payload check above cannot tell one scenario's app from another's -- a golden flash
            # that silently does not take leaves the PREVIOUS scenario's app in place and the run
            # measures the wrong thing until it times out. Seen on the N6: `watchdog_bite` ran the
            # `watchdog` app (identical but for the stop-feeding step), looked perfectly healthy on
            # the UART, and failed 15 minutes later on wdt.bit/wdt.stop that could never appear.
            if want_app is not None:
                tags = [ln.split("app: scenario ", 1)[1].strip()
                        for ln in fresh if "app: scenario " in ln]
                if tags and tags[-1] != want_app:
                    raise RuntimeError(
                        "%s is running the %r bench app but this scenario needs %r -- the golden "
                        "flash did not take. Both goldens are 1.0.0, so the version check cannot "
                        "see this." % (board, tags[-1], want_app))
            ids = [ln.split("app: device_id ", 1)[1].strip()
                   for ln in _CAP.raw if "app: device_id " in ln]
            if ids:
                log("verify: golden mounted (payload %s); device_id %s" % (payload, ids[-1]))
                return ids[-1]
        time.sleep(2)
    # Do not fail the run on the WATCHING path alone. If the markers did not arrive -- a boot that
    # landed at the very edge of the budget, a capture that lost lines -- fall back to the REPL
    # verify every other board uses. It costs the running app (mpremote Ctrl-C's it), which is
    # harmless here: run_cycle hard-resets before the scored window anyway.
    log("verify: no mount+device_id on the UART within %ds -- falling back to the REPL verify"
        % budget)
    log("verify: last UART lines were:\n%s" % "\n".join(_CAP.raw[-20:]))
    return verify_golden()


def verify_golden():
    """Confirm golden booted + mounted a valid romfs, and read the device_id in the SAME early exec.
    Returns the device_id.

    Folding the id read in here (rather than a later, separate device_exec) is what makes the wdt
    scenarios work on fast-boot boards. Golden's app arms a 100 ms watchdog once network bring-up
    finishes; after that, any REPL drop stops the feed loop and the watchdog bites -> the port drops
    and mpremote fails. This exec runs at BOOT -- /rom mounts seconds before the network is up -- so
    it lands inside the pre-arm window (and self-heals: a bite just resets the board, and a retry
    catches the next fresh boot). A separate device_id() call ran later, raced the arm, and lost on
    the RT (fast WiFi) -- Errno 5 before the OTA cycle even started."""
    log("verify: golden boots + /rom mounts + main.py present (uncompiled) + device_id")
    last = ""
    for _ in range(8):                       # the board may still be (re)booting after a flash
        time.sleep(5)
        try:
            _rc, last = device_exec(
                'import os, openmv_ota; r=os.listdir("/"); '
                'print("ROMOK", ("rom" in r) and ("main.py" in os.listdir("/rom"))); '
                'print("DEVID", openmv_ota.identity().get("device_id"))',
                timeout=30, check=False)
            if "ROMOK True" in last:
                for line in last.splitlines():
                    if line.startswith("DEVID "):
                        return line.split(" ", 1)[1].strip()
                raise RuntimeError("golden mounted romfs but reported no device_id:\n" + last)
        except Exception as e:
            last = str(e)
    raise RuntimeError("golden did not mount a valid romfs:\n" + last)


def dirty_coproc_partition():
    """Overwrite the start of the coprocessor partition (index 1) so it DIFFERS from the bundled
    coproc romfs, forcing the app's sync() to RE-APPLY it (the partition.prepare/write +
    sync.applying/applied path) rather than skip. MRAM persists the partition across golden flashes
    -- once applied it matches the bundle forever -- so the coproc scenario must actively dirty it to
    stay deterministic (else it degrades to the coproc_skip path). Writes 0xFF over the first block
    via the ranged rom_ioctl the OTA installer uses; sync() then rewrites the real romfs back."""
    device_exec(
        "import vfs\n"
        "vfs.rom_ioctl(3, 1, 0, 4096)\n"            # ranged WRITE_PREPARE the first block (idx 1)
        "vfs.rom_ioctl(4, 1, 0, b'\\xff' * 4096)",  # 0xFF -> no longer the coproc romfs magic
        timeout=30)


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
    if variant in ("full", "corrupt_sha"):
        # Force a full (non-delta) release: point --delta-from at an empty dir so no golden
        # resolves (build_ota_romfs -> "full image only"), and the device installs the full rep.
        # corrupt_sha needs a full image so the tamper can hit the sha256 gate cleanly (a delta's
        # decompressed patch has structure a naive flip would break at the parser, not the sha).
        nodelta = tempfile.mkdtemp(prefix="hil-nodelta-")
        build += ["--delta-from", nodelta]
        # A full-only build does NOT produce a .delta.gz, but a prior delta build left one in
        # the build dir -- and `client publish` uploads every artifact present, so the server
        # rejects (delta uploaded, manifest declares none). Drop the stale delta first.
        # (Belt and braces: `client publish` now uploads only what the SIGNED manifest declares,
        # so a stale delta on disk is ignored rather than rejected by the server. Clearing it
        # keeps the build dir honest, and the glob covers the v2 per-base naming.)
        sh("rm -f %s/build/%s-ota.delta*.gz" % (CFG["project"], board), check=False)
    subprocess.run(build, env=penv, check=True, timeout=900)
    _s = artifact_sizes(board)   # log the real download sizes -> ota_metrics.py folds them per run
    log("OTA sizes: manifest=%s  full=%s  delta=%s%s"
        % (_human(_s.get("manifest")), _human(_s.get("full_img_gz")), _human(_s.get("delta_gz")),
           ("  (delta=%.1f%% of full)" % (100.0 * _s["delta_gz"] / _s["full_img_gz"])
            if _s.get("delta_gz") and _s.get("full_img_gz") else "")))
    subprocess.run([ota("openmv-ota"), "client", "publish", CFG["project"], "-b", board,
                    "--server", CFG["server"], "--token", CFG["token"], "--allow-republish",
                    "--rollout", "__default__:100"], env=penv, check=True, timeout=180)
    if variant == "corrupt":
        _tamper(board, "image")        # post-erase integrity failure -> retry -> golden BACK
    elif variant == "corrupt_sha":
        _tamper(board, "image_body")   # decompresses fine, sha256 mismatches -> retry -> golden BACK
    elif variant == "bad_sig":
        _tamper(board, "manifest")     # pre-erase signature failure -> reject, stays golden
    elif variant == "bad_key":
        _tamper(board, "manifest_key")  # pre-erase untrusted-key failure -> reject, stays golden


def tamper_manifest_bytes(data, which):
    """Corrupt ONE field of a manifest.bin (in place), then re-seal the trailing crc32, so the
    device reaches the specific reject gate we mean to test. A naive mid-stream flip lands in the
    header (key_id -> "untrusted key") or body (-> "crc mismatch") depending on manifest size,
    rejecting BEFORE the boundary under test is ever reached.
      which="manifest"     -> flip a SIGNATURE byte: parse + key lookup pass, verify() fails.
      which="manifest_key" -> flip a KEY_ID byte: parse passes, the key lookup misses.
    Shared by the server-store tamper (_tamper) and the file-transport legs, which tamper the
    local artifact before staging it on the SD card. Returns the flipped offset."""
    import struct
    import binascii
    hdr = "<4sIIIIi"                                 # magic, hver, body_size, sig_size, key, alg
    hsize = struct.calcsize(hdr)                     # 24; key_id field at offset 16
    _, _, body_size, sig_size, _, _ = struct.unpack_from(hdr, data, 0)
    body_end = hsize + body_size + sig_size          # crc covers data[:body_end]
    off = 16 if which == "manifest_key" else hsize + body_size   # key_id vs signature region
    data[off] ^= 0xFF                                # break exactly that field, nothing else
    crc = binascii.crc32(bytes(data[:body_end])) & 0xFFFFFFFF
    struct.pack_into("<I", data, body_end, crc)      # re-seal so parse (+ key, for sig) pass
    return off


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
    if which in ("manifest", "manifest_key"):
        # Corrupt one FIELD, then re-seal the trailing crc32, so the device reaches the
        # specific reject gate we mean to test. A naive mid-stream flip lands in the header
        # (key_id -> "untrusted key") or body (-> "crc mismatch") depending on manifest size,
        # rejecting BEFORE the target check runs -- the boundary we mean to test is never hit.
        #   which="manifest"     -> flip a SIGNATURE byte: parse + key pass, verify() fails (682).
        #   which="manifest_key" -> flip a KEY_ID byte: parse passes, key lookup misses (680).
        target = "%s/manifests/%s/manifest.bin" % (root, rel)
        with open(target, "r+b") as f:
            data = bytearray(f.read())
        off = tamper_manifest_bytes(data, which)
        with open(target, "r+b") as f:
            f.write(data)
        log("  tampered %s byte@%d of %s (crc re-sealed)" % (which, off, os.path.basename(target)))
        return
    if which == "image_body":
        # Flip a byte in the DECOMPRESSED image, then re-gzip: it decompresses cleanly (unlike
        # the mid-stream flip below), but its sha256 no longer matches the SIGNED (unchanged)
        # manifest -> the device's integrity gate (the sha256 trust boundary, install.reject_sha)
        # rejects it AFTER the FRONT erase -> retries exhaust -> golden BACK. Needs a full image
        # (see publish_update): a delta's decompressed patch has structure a flip would break at
        # the patch parser, not the sha. Size is unchanged (a 1-byte flip), so only the sha moves.
        import gzip
        # Select the FULL image by its own name. "not *.delta.gz" was the v1 test and it no
        # longer excludes anything -- v2 deltas are named `-ota.delta-<base>.gz`.
        fulls = [p for p in imgs
                 if os.path.dirname(p).endswith(rel) and p.endswith("-ota.img.gz")]
        target = fulls[-1] if fulls else newest
        with open(target, "rb") as f:
            raw = bytearray(gzip.decompress(f.read()))
        mid = len(raw) // 2
        raw[mid] ^= 0xFF
        with open(target, "wb") as f:
            f.write(gzip.compress(bytes(raw)))
        log("  tampered image_body byte@%d of %s (re-gzipped; sha256 now mismatches)"
            % (mid, os.path.basename(target)))
        return
    # image: mid-stream flip -> the download decompress/sha256 fails AFTER the slot erase.
    #
    # EVERY representation gets flipped, not just one. A v2 release ships a full image AND one
    # delta per base version, and the device picks whichever matches the release it is running
    # -- so corrupting a single artifact leaves the scenario at the mercy of that choice. It
    # already bit: this used to target `*.delta.gz`, the v1 name, so under v2 it flipped the
    # full image while the device happily installed the untouched delta and the scenario failed
    # with `install.armed` where it expected a fallback. Tampering the whole set makes the test
    # say what it means -- "the image this device downloads is corrupt" -- whatever it picks.
    reps = [p for p in imgs if os.path.dirname(p).endswith(rel)]
    for target in reps:
        with open(target, "r+b") as f:
            f.seek(0, 2)
            n = f.tell()
            mid = n // 2                                 # flip one byte mid-stream
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


# ---------------------------------------------------------------------------
# File-transport cycle -- the classic boards (OPENMV2/3/4, single-image mode).
#
# These builds have no TLS stack (the F427 has no `ssl` module at all), so their real-world
# update path is install() from a file on the SD card -- and that is exactly what this cycle
# exercises, end to end on hardware: build golden 1.0.0, flash it over DFU, build the signed
# update artifacts, stage them on the SD card over the USB-CDC, run
# install("/sdcard/<board>-manifest.bin"), and probe the outcome after the reboot.
#
# No server, no marker UART: the evidence is the CDC. The install's own log lines land in the
# exec output (openmv_log ENABLED -> the USB REPL), and the post-boot status probe says whether
# the version moved and the trial confirmed. That is the same evidence the manual bring-up used
# to prove all three boards.
# ---------------------------------------------------------------------------
def file_bench_main_py():
    """The classic boards' bench app: confirm a fresh trial, then idle. No network bring-up and
    no run() -- the harness drives install() over the CDC -- and kept deliberately tiny: the M4
    has a 39 KB heap, so every statement this app runs before confirm() is heap the trial boot
    must afford. The prints land on the USB REPL where the harness's probes can see them."""
    return (
        "import time\n"
        "import openmv_ota\n"
        "try:\n"
        "    st = openmv_ota.status()\n"
        "    print('BENCH boot', st.get('payload_version'), st)\n"
        "    if st.get('trial'):\n"
        "        openmv_ota.confirm()\n"
        "        print('BENCH confirmed')\n"
        "except Exception as e:\n"
        "    print('BENCH app error', repr(e))\n"
        "while True:\n"
        "    time.sleep(1)\n"
    )


def file_prepare(board, checkout):
    """prepare()'s file-transport twin: refresh the project's vendored runtime from the checkout
    under test, turn the device log on (to the USB REPL -- no marker UART on these nodes), and
    write the minimal confirm-on-trial app."""
    log("prepare: refresh vendored runtime + file bench app")
    dev = checkout + "/src/openmv_ota/build/device"
    sh("cp -rf %s/openmv_ota/. %s/app/lib/openmv_ota/" % (dev, CFG["project"]))
    sh("cp -rf %s/openmv_cloud/. %s/app/lib/openmv_cloud/ 2>/dev/null || true" % (dev, CFG["project"]))
    sh("mkdir -p %s/device && cp -f %s/*.py %s/device/" % (CFG["project"], dev, CFG["project"]))
    # ENABLED=True with UART=None -> the log goes to sys.stdout (the USB REPL), so the install's
    # own account ("install: installed + armed", "install: reject bad signature") arrives inside
    # the mpremote exec output this cycle scores. Fail LOUD if the sed didn't take, exactly like
    # prepare()'s watchdog flip -- a silent miss would score a healthy install as evidence-free.
    logpy = "%s/device/openmv_log.py" % CFG["project"]
    sh("sed -i 's/^ENABLED = False/ENABLED = True/' " + logpy)
    sh("grep -q '^ENABLED = True' " + logpy)
    open(CFG["project"] + "/app/main.py", "w").write(file_bench_main_py())
    _ensure_cdc(board, allow_erase=True)     # no-op today (no J-Link on these nodes); keeps the shape


def file_publish(board, version):
    """Build the signed update artifacts locally (there is no server to publish to):
    <board>-manifest.bin + <board>-ota.img.gz in the project's build/. Always a FULL image --
    single-image mode has no delta base to patch against after the slot is erased."""
    log("build: ota-romfs %s (update %s)" % (board, version))
    set_version(version)
    penv = dict(os.environ, PATH=CFG["sdk"] + "/make:" + os.environ["PATH"])
    subprocess.run([ota("openmv-ota"), "build", "ota-romfs", CFG["project"], "-b", board,
                    "--allow-dev-key", "--allow-republish"],
                   env=penv, check=True, timeout=900)


def file_stage(board, tamper=None):
    """Stage the artifacts on the board's SD card over the USB-CDC (mpremote cp), optionally
    tampering the manifest's signature first -- the SAME byte the server-store tamper flips, so
    the file leg reaches the same reject gate (verify() fails; parse + key lookup pass). Returns
    the on-device manifest path install() is given."""
    bdir = "%s/build" % CFG["project"]
    man = "%s/%s-manifest.bin" % (bdir, board)
    img = "%s/%s-ota.img.gz" % (bdir, board)
    if tamper is not None:
        data = bytearray(open(man, "rb").read())
        off = tamper_manifest_bytes(data, tamper)
        man = "%s/%s-manifest-tampered.bin" % (bdir, board)
        open(man, "wb").write(bytes(data))
        log("stage: tampered %s byte@%d (crc re-sealed)" % (tamper, off))
    dev_man = "/sdcard/%s-manifest.bin" % board
    log("stage: cp image + manifest -> /sdcard (USB-CDC)")
    _mpremote(["cp", img, ":/sdcard/%s" % os.path.basename(img)], timeout=300)
    _mpremote(["cp", man, ":" + dev_man], timeout=120)
    return dev_man


def _wait_cdc(budget=120):
    """Wait for the USB-CDC to answer after a reboot. The probe itself DTR-resets the board,
    which is fine here: the app confirms a trial at BOOT, so every extra boot re-runs it."""
    deadline = time.time() + budget
    while time.time() < deadline:
        rc, _ = sh([ota("mpremote"), "connect", CFG["acm"], "eval", "True"],
                   timeout=15, check=False, quiet=True)
        if rc == 0:
            return True
        time.sleep(3)
    return False


def _payload_version(vstr):
    """The encoded payload_version int for an app-version string -- what status() reports and
    what the manifest/trailer carry. Imported from the installed tool so the harness can never
    drift from the real encoding."""
    from openmv_ota.ota.version import encode_app_version
    return encode_app_version(vstr)


def file_probe(retries=3):
    """(payload_version, confirmed, trial) off the board's own status(), over the CDC. Retries
    transport errors only -- the parsed answer, whatever it says, is the verdict.

    payload_version, NOT a version string: status() carries the ENCODED int (there is no
    "version" key in it -- the app-version string lives in identity()). Callers compare against
    encode_app_version(target), the same encoding the manifest and trailer carry."""
    code = ("import openmv_ota\n"
            "s = openmv_ota.status()\n"
            "print('PROBE|%s|%s|%s' % (s.get('payload_version'), s.get('confirmed'), "
            "s.get('trial')))\n")
    for attempt in range(retries):
        rc, out = device_exec(code, timeout=90, check=False)
        for line in reversed(out.splitlines()):
            if line.startswith("PROBE|"):
                _, v, confirmed, trial = line.strip().split("|")
                return int(v), confirmed == "True", trial == "True"
        log("  (probe attempt %d/%d got no PROBE line, retrying)" % (attempt + 1, retries))
        time.sleep(5)
    raise RuntimeError("status probe never answered over the CDC:\n%s" % out[-1500:])


def run_file_scenario(args, spec, trace, phase):
    """Drive one file-transport scenario end to end. Returns a run_cycle-shaped result dict;
    the caller's shared scoring (empty expect/forbid) reduces to result["reached_end"]."""
    board = args.board
    phase("prepare", lambda: file_prepare(board, args.checkout))
    if not args.skip_provision:
        phase("build_golden", lambda: build_golden(board))
        phase("flash_golden", lambda: flash_golden(board))
    if not _wait_cdc():
        raise RuntimeError("no CDC after the golden flash -- board never enumerated")
    golden_v = _payload_version("1.0.0")
    pre_v, _, _ = file_probe()
    log("golden up: payload_version %d" % pre_v)
    if pre_v != golden_v:
        raise RuntimeError("golden probe says payload_version %r, expected %d (1.0.0) -- the "
                           "flash did not take" % (pre_v, golden_v))
    tamper = "manifest" if args.scenario == "file_bad_sig" else None
    if not args.skip_publish:
        phase("publish", lambda: file_publish(board, args.target))
        trace["metrics"] = artifact_sizes(board)
    dev_man = phase("stage", lambda: file_stage(board, tamper=tamper))
    # install() REBOOTS the board on success, so the exec dies with a transport error -- that is
    # the expected shape of a PASS, and why check=False. Its output up to the reboot carries the
    # device's own account (openmv_log -> USB REPL), kept in the trace as the run's device log.
    log("install: exec openmv_ota.install(%r)" % dev_man)
    code = "import openmv_ota\nopenmv_ota.install(%r)\n" % dev_man
    s = time.time()
    _, out = device_exec(code, timeout=args.timeout, check=False)
    trace["phases"]["install"] = round(time.time() - s, 1)
    trace["log"] = out.splitlines()
    for ln in out.splitlines()[-25:]:
        if ln.strip():
            log("  [cdc] " + ln.rstrip())
    if spec["end"] == "promoted":
        # PASS = after the reboot the board is RUNNING the target, confirmed -- the post-boot
        # probe is the verdict. The install's own "installed + armed" line is CORROBORATING
        # evidence only, never a requirement: the F4's CDC DROPS buffered output at reset
        # (measured on the M4 -- a flawless install can end with an empty exec capture). What a
        # capture CAN prove is the opposite: a traceback with no armed line means install()
        # raised and the board never rebooted, so fail on the device's own words instead of
        # waiting out a probe of the unchanged golden.
        if "Traceback" in out and "installed + armed" not in out:
            why = [ln for ln in out.splitlines() if ln.strip()][-1].strip()
            return {"saw_golden": True, "saw_target": False, "version": pre_v, "slot": None,
                    "reached_end": False, "why": "install raised: " + why}
        time.sleep(10)                       # let the trial boot run the app's confirm() first
        if not _wait_cdc():
            return {"saw_golden": True, "saw_target": False, "version": None, "slot": None,
                    "reached_end": False, "why": "no CDC after the install's reboot"}
        target_v = _payload_version(args.target)
        v, confirmed, trial = file_probe()
        if trial and not confirmed:
            # The probe's Ctrl-C may have beaten the app's confirm() on this boot. One clean
            # reboot gives the app a fresh boot to confirm on (attempts allow it), then re-probe.
            log("  trial not yet confirmed -- one reset so the app gets a clean boot")
            device_exec("import machine; machine.reset()", timeout=20, check=False)
            time.sleep(12)
            _wait_cdc()
            v, confirmed, trial = file_probe()
        ok = (v == target_v) and confirmed
        # slot stays None: single-image mode has ONE slot, and the probe doesn't read a slot
        # name -- inventing "A" here would be data the device never said.
        return {"saw_golden": True, "saw_target": v == target_v, "version": v, "slot": None,
                "reached_end": ok,
                "why": "-" if ok else "post-boot probe: payload_version=%s confirmed=%s"
                                      % (v, confirmed)}
    # end == "golden": the tampered manifest must be REFUSED pre-erase -- install() raises to
    # the caller (the exec completes; no reboot), the device logs the reject, and the version
    # must not move. "signature does not verify" is the raise; "reject bad signature" the log.
    refused = ("signature does not verify" in out) or ("reject bad signature" in out)
    erased = ("install: staged" in out) or ("installed + armed" in out)
    v, confirmed, _ = file_probe()
    ok = refused and not erased and v == golden_v
    return {"saw_golden": True, "saw_target": False, "version": v, "slot": None,
            "reached_end": ok,
            "why": "-" if ok else ("refused=%s erased=%s payload_version=%s"
                                   % (refused, erased, v))}


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


def run_cycle(devid, golden, target, end, expect, cap, timeout_s, by_marker=False,
              after_reset=None):
    """Hard-reset the device and watch the server record + UART until the scenario's end
    state is reached (early exit) or the timeout elapses. Returns the observed state; the
    caller decides PASS/FAIL against the scenario's expect/forbid sets.

    end="promoted": the device ends on the TARGET, confirmed on FRONT (the happy paths).
    end="golden":   the device stays on / falls back to the GOLDEN (the negative paths -- an
                    update refused pre-erase, or installed then rolled back / fell back). The
                    device may loop (re-offer -> re-fail -> golden), so we exit once it has
                    SETTLED back on golden with every expected marker seen."""
    log("cycle: hard reset -> autonomous run; end=%s; watching UART + server" % end)
    # RESET THROUGH THE DEBUG CORE wherever there is a J-Link. Two reasons, and the second is the
    # one that bites:
    #
    #  1. it needs no CDC, so it still works when the port has not come back yet -- precisely when
    #     the scored window would otherwise never start;
    #  2. asking for the reset over mpremote means taking the REPL with a Ctrl-C FIRST, and on a
    #     board whose app has ARMED THE WATCHDOG that stops the feed. The watchdog then bites before
    #     machine.reset() ever runs, so the board boots with reset_cause == 3 (watchdog) instead of
    #     a software reset. wdt_bite's app reads that as "I have already bitten, recover and feed",
    #     skips the bite sequence entirely, and the scenario fails with wdt.bit and wdt.stop
    #     missing -- exactly the observed failure. The harness was choosing the reset cause it was
    #     about to measure.
    #
    # A core reset asks the processor directly and never touches the app, so the cause the scenario
    # sees is the one the scenario arranged.
    # CONFIRM the reset, do not trust it. jlink_core_reset returns True whenever the board merely
    # HAS a J-Link -- it runs JLinkExe with check=False, so a connect that fails looks identical to
    # a reset that landed. Measured: it resets the Portenta reliably and does NOT reset the N6, and
    # trusting it there opened a scored window on a board that never rebooted -- 0/5 markers for the
    # full timeout, with the server record still showing the pre-reset state. Watch for the board's
    # own boot marker; if it does not come, fall back to the REPL reset, which is worse for an armed
    # watchdog but is at least a reset.
    if (_BOARD and BOARDS[_BOARD].get("jlink_device") and jlink_core_reset(_BOARD)
            and _await_boot(_BOARD, budget=60)):
        pass                                 # CONFIRMED: it actually rebooted; no REPL was taken
    elif (_BOARD and BOARDS[_BOARD].get("jlink_device")
          and BOARDS[_BOARD].get("flash") != "arduino_cli"
          and jlink_reset_pulse(_BOARD) and _await_boot(_BOARD, budget=60)):
        # The core reset did not take. Try the RESET PIN, which is the lever that has always worked
        # on these boards (it is what _ensure_cdc uses to revive a wedged N6/AE3). NOT for the
        # Arduino boards: there the pin can land the board back in its DFU bootloader, because the
        # 1200-baud touch's stay-in-bootloader flag lives in RAM and survives it.
        log("cycle: %s reset via the nRST pin (the core reset did not take)" % _BOARD)
    else:
        if _BOARD and BOARDS[_BOARD].get("jlink_device"):
            log("cycle: no SWD reset produced a boot on %s -- falling back to the REPL reset, which "
                "Ctrl-Cs the app and so bites an armed watchdog" % _BOARD)
        try:                                 # machine.reset() drops the USB-CDC -> mpremote
            device_exec("import machine; machine.reset()", timeout=20, check=False)
        except Exception:
            pass                             # ...an I/O error here just means the reset landed
    # Some boards are served OTA but never RECORDED: the server's `unverified_boards` set skips the
    # device-registry write entirely, so device_record() returns nothing for them no matter how well
    # the install goes. Waiting on that record means never concluding -- the run watches until its
    # timeout while the device, left running, re-installs over and over. Score their UART markers
    # instead; they are the same evidence the scenario's expect/forbid sets are written against.
    # PUBLISH HERE, NOT BEFORE THE RESET, when the caller asks for it. An update that is already
    # offerable when this function is entered can be picked up by the device in the seconds between
    # the publish and the reset above -- the device polls every ~5 s -- and then the reset lands
    # MID-INSTALL. Measured on RT1060 lan `reinstall` phase 2: offered at :38, erasing at :39, the
    # UART severed mid-word, the board back 66 s later with `boot: rejected A:magic -> mounted B`.
    # The scenario then cannot pass: it wants install.reject_sha, but the install never reached the
    # sha check, and the half-erased slot leaves the device unsettled so the server (correctly)
    # refuses to offer again -- the leg deadlocks for its whole window.
    #
    # This is a RACE, not a certainty: the same scenario passes whenever the reset wins. It passed
    # on the bench (reset :31, offer :31) and failed in CI (offer :38, reset :39) on identical code,
    # which is exactly why it must be closed by ORDERING rather than by a delay.
    #
    # Phase 1 has the same shape and is left alone deliberately: there the device is on golden, so
    # an install the reset interrupts is simply retried inside the window -- self-healing. Only a
    # phase starting from a PROMOTED board deadlocks, because there the interrupted install costs
    # the slot it was writing.
    if after_reset is not None:
        after_reset()
    by_marker = by_marker or (bool(_BOARD) and not BOARDS[_BOARD].get("server_record", True))
    if by_marker:
        log("cycle: %s is not recorded server-side -- scoring the UART markers" % _BOARD)
    deadline = time.time() + timeout_s
    last = None
    saw_golden = saw_target = False
    v = slot = None
    hb = 0
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
            hb = 0
        else:
            hb += 1
            if hb % 4 == 0:                        # ~60s with no new state -> a heartbeat, so a long
                log("  ... still watching (%ds elapsed, %d/%d markers, on %s/%s)"   # erase/download
                    % (int(time.time() - (deadline - timeout_s)), len(marks), len(expect), v, slot))
        if by_marker:
            if have:
                break                        # no server record to corroborate: the device's own
                #                              markers ARE the evidence, and they are complete
        elif end == "promoted":
            if v == target and slot and have:
                break                        # real pre-update -> target transition, all paths hit
        elif saw_golden and v == golden and have:
            break                            # settled back on golden, all negative paths hit
    # NOT `slot == "FRONT"`. Under v2 an update lands in whichever slot the device was not
    # running, so the promoted image is legitimately in A on one cycle and B on the next --
    # pinning the name here would fail every happy path on a board that happens to be running
    # from B. What the assertion actually needs is that a slot was selected and the VERSION
    # moved, which is what the two clauses below check.
    # DO NOT require `saw_golden` on the promoted path. It is a POLLED observation of a TRANSIENT
    # state: the harness reads the server's device record every 15 s and has to catch the device
    # reporting golden before the install flips it to target. That is a race against install speed,
    # and it is now lost -- the blank-skip erase took the RT1060's slot erase from 54 s to 4 s, so a
    # delta install can start and finish inside a single poll gap. The record then only ever reads
    # target, `saw_golden` never latches, the break above can never fire, and the leg burns its whole
    # window and scores a FAILURE for an install that was flawless. Measured: RT1060 lan `delta` and
    # `watchdog` both went 362 s/PASS -> 606 s/FAIL on exactly the run that made installs faster,
    # with every expected marker present and nothing forbidden.
    #
    # `have` is the stronger witness anyway, and it is not racy. The capture is reset when the window
    # opens (see cap.reset below the publish), so every marker scored here was produced INSIDE this
    # window -- and a promoted scenario's expect set runs install.start -> install.committed ->
    # confirm.promoted. A device that was already on target and did nothing cannot produce those, so
    # dropping the polled check does not weaken the "it really transitioned here" assertion that
    # saw_golden existed to make; it just stops asserting it with a stopwatch.
    #
    # Polling faster would only move the race (a 2 s install would still be missed). end="golden"
    # keeps saw_golden: those legs never install, so nothing shrinks that state.
    reached = (have if by_marker else
               ((end == "promoted" and v == target and bool(slot) and have)
                or (end == "golden" and saw_golden and v == golden)))
    return {"saw_golden": saw_golden, "saw_target": saw_target,
            "version": v, "slot": slot, "reached_end": reached}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", required=True, choices=sorted(BOARDS))
    ap.add_argument("--checkout", default=env("GITHUB_WORKSPACE", os.getcwd()))
    ap.add_argument("--target", default="1.1.0", help="the update version to install")
    ap.add_argument("--timeout", type=int, default=int(env("HIL_TIMEOUT", "600")))
    ap.add_argument("--trace", default=env("HIL_TRACE", "hil-trace.json"))
    ap.add_argument("--network", choices=["lan", "wifi", "file"], default=None,
                    help="override the board's default network for the bench app (e.g. N6 wifi); "
                         "'file' is the classic boards' SD-card transport, never an override")
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), default="delta",
                    help="which OTA path to exercise (see SCENARIOS): delta/full happy paths, "
                         "corrupt/rollback/bad_sig/bad_version negative paths")
    ap.add_argument("--skip-provision", action="store_true",
                    help="reuse the already-flashed golden (skip build/flash/verify). Use WITH "
                         "--skip-publish: a fresh publish rebuilds the golden, and a delta's base "
                         "must match the flashed golden or the install fails the sha256 check.")
    ap.add_argument("--skip-publish", action="store_true",
                    help="reuse the already-published update")
    ap.add_argument("--list-regression", action="store_true",
                    help="print (space-separated) the scenarios this board+network runs in the "
                         "full regression, then exit -- the hil-ota workflow loops over these")
    args = ap.parse_args()

    network = args.network or BOARDS[args.board]["network"]
    if args.list_regression:
        print(" ".join(regression_scenarios(args.board, network)))
        return 0
    spec = SCENARIOS[args.scenario]
    # The file scenarios and the file boards go together, both ways: a network board asked for
    # file_full has no SD-staged artifact path, and a file board asked for delta has no server
    # to poll. Refuse the mismatch loudly rather than timing out into a fake board failure.
    if (network == "file") != args.scenario.startswith("file_"):
        ap.error("scenario %r does not run on network %r (file scenarios <-> file boards)"
                 % (args.scenario, network))
    expect, forbid = scenario_markers(args.board, args.scenario)
    pub_version = spec.get("version", args.target)     # bad_version publishes below the floor
    t0 = time.time()
    global _BOARD
    _BOARD = args.board
    trace = {"board": args.board, "network": network, "scenario": args.scenario,
             "target": args.target, "end": spec["end"], "passed": False,
             "expect": sorted(expect), "forbid": sorted(forbid), "markers": [], "phases": {}}
    cap = None
    srv = None

    def phase(name, fn):
        s = time.time()
        r = fn()
        trace["phases"][name] = round(time.time() - s, 1)
        return r

    try:
        log("board %s, network %s, scenario %s (%s)"
            % (args.board, network, args.scenario, spec["desc"]))
        # Each rig spins up its OWN update server for this run (self-contained; no shared bench
        # server, tamper scenarios work on every board). Point CFG at it BEFORE prepare(), which
        # bakes the URL into the bench app + copies this run's CA onto the board.
        # Only bad_version wants the relaxed offer gate; see bench_server.start.
        # The file legs skip it entirely: no TLS on those builds means no server to reach, and
        # their artifacts are built locally + staged over the CDC (see run_file_scenario).
        if network != "file":
            srv = bench_server.start(ota("python"), log=log,
                                     offer_downgrades=(args.scenario == "bad_version"))
            CFG["server"], CFG["ca_node"], CFG["artifacts"], CFG["token"] = (
                srv["url"], srv["ca"], srv["store"], srv["token"])
        if network == "file":
            # No server record and no marker UART: the classic legs score the device's own CDC
            # account. expect/forbid are empty for these scenarios, so the shared scoring below
            # reduces to result["reached_end"].
            result = run_file_scenario(args, spec, trace, phase)
        elif spec["end"] == "no_slot":
            # No OTA: brick BOTH romfs slots, then watch for boot.py's 'no bootable slot'. Start
            # capture BEFORE the brick flash so the reset it triggers (-> boot -> the log line)
            # is caught. Requires the board already provisioned + bootable (firmware carries the
            # bench logger and /flash/.hilcov_uart is set) -- run it after another scenario.
            cap = UartCapture(CFG["uart"])
            cap.start(time.time())
            phase("flash_brick", lambda: flash_golden(args.board, bad_romfs=True))
            result = run_cycle_no_slot(cap, expect, args.timeout)
        else:
            devid = None
            # Start listening BEFORE provisioning. Everything up to here talks to the board over the
            # USB-CDC, so when the CDC is what breaks, the harness gives up before it ever opens the
            # one channel that still works -- and the board's own account of why it died is lost.
            # (Debugging the H7 Plus watchdog leg cost several runs to exactly this: "golden did not
            # mount a valid romfs" with coverage 0/N, while the board was on the UART the whole time
            # saying something else entirely.) The scored window is unchanged: cap.reset() below
            # drops these provisioning-phase markers before the scenario is judged.
            cap = UartCapture(CFG["uart"])
            cap.start(time.time())
            if not args.skip_provision:
                phase("prepare", lambda: prepare(args.board, args.checkout, network, spec["app"]))
                phase("build_golden", lambda: build_golden(args.board))
                phase("flash_golden", lambda: flash_golden(args.board))
                # VERIFY OFF THE UART ON EVERY BOARD. The mpremote route takes the REPL with a
                # Ctrl-C, and on a board whose app has ARMED THE WATCHDOG that is fatal: the feed
                # stops, the watchdog bites, the board reboots, the app re-arms, the next probe
                # kills it again. Captured on the RT:
                #     wdt armed=True / wdt: feed / app: CRASHED KeyboardInterrupt()
                #     app: booting ... reset_cause=3      <- 3 = watchdog reset
                #     wdt armed=True / wdt: feed / app: CRASHED KeyboardInterrupt()
                # The harness was fighting the very watchdog the scenario exists to test. Watching
                # cannot do that. Falls back to the REPL verify by itself when there is no capture
                # or the markers do not arrive, so a board without a marker UART is unaffected.
                devid = phase("verify_golden",
                              lambda: verify_golden_uart(args.board, want_app=spec["app"]))
            if devid is None:                    # --skip-provision: board already up, read it directly
                devid = device_id()
            trace["device_id"] = devid
            log("device_id: " + devid)
            if args.scenario == "coproc":        # dirty partition 1 so sync() APPLIES (not skips)
                phase("dirty_coproc", dirty_coproc_partition)
            # DEFERRED UNTIL AFTER THE WINDOW'S RESET -- see the after_reset note in run_cycle.
            # Publishing here, eagerly, leaves the update offerable while the board is still running,
            # and the device (polling every ~5 s) can start installing in the seconds before the
            # reset lands. The reset then kills that install MID-ERASE. Phase 2 was fixed first and
            # phase 1 was left alone on the argument that it is self-healing -- the device is on
            # golden, so it falls back and retries. That is true of the DEVICE and false of the
            # SCORING: `delta` FORBIDS boot.fallback, so the leg fails on a board that behaved
            # perfectly. Measured on RT1060 lan: erase at :19, the UART severed mid-line, reboot at
            # :22, `boot: rejected B:body-sha -> mounted A`, missing=- forbidden=[boot.fallback].
            publish = None
            if spec["publish"] != "none" and not args.skip_publish:
                def publish():                   # noqa: E306  (runs inside run_cycle, post-reset)
                    phase("publish", lambda: publish_update(args.board, pub_version,
                                                            spec["publish"]))
                    trace["metrics"] = artifact_sizes(args.board)  # sizes -> ota_metrics report
            # THE MARKER UART MUST BE ALIVE BEFORE THE SCORED WINDOW OPENS. Every scenario is scored
            # on lines from this stream; if it is dead, the run takes its full timeout and then
            # reports a pile of missing markers -- which reads as a broken device and is not.
            # Observed: a leg whose `.hilcov_uart` never landed (prepare deferred the bench files
            # after a recovery erase, and the deferred write did not happen) produced ZERO device
            # lines for 25 minutes, then failed with boot.ready/log.configured missing. The board was
            # fine; it was logging to USB because nothing told it which UART to use.
            if not _uart_live_since_flash():
                raise RuntimeError(
                    "no device output on the marker UART (%s) since golden was flashed -- the board is "
                    "logging somewhere else. Check that /flash/.hilcov_uart exists and names UART %s "
                    "(prepare writes it; a recovery erase DEFERS that write), and that nothing else "
                    "holds the port." % (CFG["uart"], BOARDS[args.board]["cov_uart"]))
            cap.reset(time.time())               # scored window starts HERE (see the early start above)
            # "install" phase = the whole autonomous OTA (check-in -> download -> write -> trial ->
            # confirm/promote or fallback). Timing it makes install SPEED a tracked metric too.
            result = phase("install", lambda: run_cycle(
                devid, "1.0.0", args.target, spec["end"], expect, cap, args.timeout,
                after_reset=publish))
        time.sleep(2)                            # let the last UART lines land
        marks = set(cap.points()) if cap is not None else set()   # file legs run capture-less
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
            log("FAIL: end=%s reached=%s missing=%s forbidden=%s%s"
                % (spec["end"], result["reached_end"], missing or "-", forbidden or "-",
                   (" why=" + result["why"]) if result.get("why", "-") != "-" else ""))
            # ...and WHY, if the board said. Missing markers name the paths that did not run; this
            # names the exception that stopped them. A board that can never install repeats one
            # fault forever, which is otherwise indistinguishable from a board with nothing to do.
            if cap is not None:
                for text, hits in device_faults(cap).items():
                    log("  the device reported this %d time(s): %s" % (hits, text))
        # A SECOND PHASE, for the paths that only exist AFTER a promote. Every scenario above
        # starts from golden, so the whole "what happens to a board that has already taken an
        # update" surface was unreachable: the run ends the moment the first cycle settles. That
        # left the re-install fallback untested across the entire fleet -- the device is on the
        # promoted image with golden in BACK, a NEW update fails its integrity gate, and where it
        # lands is the question nobody had answered on hardware.
        then = spec.get("then")
        if then and trace["passed"]:
            log("phase 2: %s" % then["desc"])
            # PUBLISHED INSIDE run_cycle, AFTER its reset -- see the after_reset note there. Doing it
            # here instead leaves the bad update offerable while the board is still running, and the
            # device (polling every ~5 s) can start installing it in the gap before the window's
            # reset lands, which then kills the install mid-erase and deadlocks the phase.
            publish2 = lambda: phase("publish2",                                       # noqa: E731
                                     lambda: publish_update(args.board, then["version"],
                                                            then["publish"]))
            cap.reset(time.time())               # score phase 2 on its own window
            expect2, forbid2 = set(then["expect"]), set(then.get("forbid", ()))
            # SCORE PHASE 2 ON ITS MARKERS, not on a version transition. The assertion is that
            # the integrity gate fires from a non-golden start and the bad image never promotes --
            # which the marker set states exactly. Waiting for the device to SETTLE back on golden
            # additionally requires the retry-exhaust fallback, and that is slot-sized: each attempt
            # writes the whole image before the sha check fails, so on the N6's 12 MiB slot three
            # attempts run past the watch window. `corrupt_sha` documents the same trap and dodges
            # it only because its device never leaves golden, making end="golden" true for free.
            # Measured: N6 lan phase 2 came back missing=- forbidden=- reached=False -- every marker
            # hit, nothing promoted, just not settled in time. RT1060 (4 MiB slot) passed.
            # PHASE 2 IS SLOT-SIZED WORK, AND PHASE 1 IS NOT. The retry-exhaust path writes the
            # WHOLE image once per attempt, up to _RETRIES of them, where phase 1 installs once --
            # so giving both the same window makes the scenario pass or fail on how big the board's
            # slot is. It did: the N6 (12 MiB slot) lands at ~1040 s against a 600 s per-phase
            # default and has been failing on the ~150 s of headroom it had left, while the RT
            # (4 MiB) passes comfortably. Measured directly: with room, the same leg PASSES in
            # 1040 s -- nothing hangs, it was only ever short of budget. Scale by the retry count
            # rather than picking a bigger constant, so the window tracks the work.
            result2 = phase("reinstall", lambda: run_cycle(
                devid, "1.0.0", then["version"], then["end"], expect2, cap,
                args.timeout * _RETRIES, after_reset=publish2,
                by_marker=then.get("by_marker", False)))
            time.sleep(2)
            marks2 = set(cap.points())
            missing2, forbidden2 = sorted(expect2 - marks2), sorted(forbid2 & marks2)
            trace["phase2"] = {"result": result2, "missing_expected": missing2,
                               "forbidden_hit": forbidden2}
            trace["passed"] = result2["reached_end"] and not missing2 and not forbidden2
            if not trace["passed"]:
                log("FAIL (phase 2): end=%s reached=%s missing=%s forbidden=%s"
                    % (then["end"], result2["reached_end"], missing2 or "-", forbidden2 or "-"))
                for text, hits in device_faults(cap).items():
                    log("  the device reported this %d time(s): %s" % (hits, text))
    except Exception as e:
        trace["error"] = str(e)
        log("ERROR: " + str(e))
        # PRINT WHAT THE BOARD SAID. Every phase above drives the board over the USB-CDC, so a
        # failure there tells you only that the harness could not talk to it -- never why. The
        # marker UART is a separate wire and keeps working when the CDC does not, so its tail is
        # usually the actual explanation ("golden did not mount a valid romfs" while the board was
        # cheerfully logging a healthy boot is a real example this would have caught immediately).
        if cap is not None:
            tail = [ln for ln in cap.tail(40) if ln.strip()]
            log("---- device UART tail (%d lines) -- the board's own account ----" % len(tail))
            for ln in tail:
                log("  [uart] " + ln.rstrip())
            if not tail:
                log("  [uart] (nothing captured -- board silent, or the marker UART is misconfigured)")
    finally:
        if cap is not None:
            cap.stop()
            trace["markers"] = cap.points()
            trace["missed"] = sorted(set(COVERAGE.values()) - set(cap.points()))
            trace["marker_trace"] = cap.markers
            trace["log"] = cap.raw                # the full device log for this run
        bench_server.stop(srv)                    # tear the per-run server + store down
        trace["elapsed_s"] = round(time.time() - t0, 1)
        # CREATE THE TRACE DIRECTORY. A whole scenario -- provision, flash, install, confirm, the
        # lot -- is thrown away here if the parent directory happens not to exist, and it fails
        # AFTER the work, so the run looks like a scenario failure rather than a missing mkdir.
        # It cost a full Portenta cycle exactly that way: the board did the entire OTA correctly
        # and the run still exited non-zero, with no RESULT line and no trace to explain it.
        d = os.path.dirname(os.path.abspath(args.trace))
        if d:
            os.makedirs(d, exist_ok=True)
        json.dump(trace, open(args.trace, "w"), indent=2)

    log("=" * 60)
    log("RESULT: %s  scenario=%s  (%.0fs)"
        % ("PASS" if trace["passed"] else "FAIL", args.scenario, trace["elapsed_s"]))
    log("coverage %d/%d: %s" % (len(trace["markers"]), len(COVERAGE), ", ".join(trace["markers"])))
    log("trace -> " + args.trace)
    return 0 if trace["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

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

import bench_server                          # ephemeral per-run update server (sibling module)

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
    # the update server's local artifact store, for tamper scenarios (corrupt/bad_sig) --
    # only reachable when the harness runs ON the server node (co-located store).
    "artifacts": env("OTA_ARTIFACTS", HOME + "/otasrv/artifacts"),
}

# The coprocessor-sync scenarios (coproc/coproc_skip, AE3-only) dirty + rewrite the coprocessor MRAM
# partition, and that write currently crashes/wedges the AE3 off USB. Keep them OUT of the default
# regression so the AE3 can run the rest of the suite; flip HIL_COPROC=1 to opt back in (for a manual
# coproc run) once the MRAM write is fixed. See regression_scenarios().
COPROC_ENABLED = env("HIL_COPROC", "") == "1"

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
    "ARDUINO_NICLA_VISION": {                # Arduino Nicla Vision (STM32H747, QSPI ROMFS dual-slot)
        "cov_uart": 4,                       # UART4 on the SDA/SCL header (J2-1=PB9 TX, J2-2=PB8 RX),
                                             # NOT the P4/P5 pads (those are SWCLK/NRST on the Nicla)
        "cov_write": "install.xip",          # stm32 XIP write path (the OTA dual-slot lives in QSPI ROMFS)
        "network": "wifi",                   # onboard CYW4343 -- standard network.WLAN (no shield)
        "flash": "arduino_cli",              # CLI's arduino backend: an automatic 1200-baud touch enters
                                             # MCUboot DFU, then address-based `dfu-util -w`; stages the
                                             # factory image as <board>-romfs.img (see _flash_arduino_cli)
        "jlink_device": "STM32H747XI_M7",    # debug-only name (M7 core runs the firmware), _ensure_cdc only
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
    "install: writing FRONT": "install.writing",             # streamed write loop starting
    "install: write path ready block-device": "write.ready",  # closures bound (def lines witnessed)
    "install: write path ready XIP": "write.ready",
    "install: erased FRONT block-device": "write.erased",     # FRONT slot erased before the write
    "install: erased FRONT XIP": "write.erased",
    "install: erasing block block-device": "write.erased",    # in-loop erase op (loop body witness)
    "install: erasing block XIP": "write.erased",
    "install: back reading block-device": "write.backread",   # in-loop BACK read (delta loop body)
    "install: wrote block block-device": "write.wrote",       # a data block written to FRONT
    "install: wrote block XIP": "write.wrote",
    "install: readback block-device": "write.readback",       # written data read back for verify
    "install: readback XIP": "write.readback",
    "install: back read block-device": "write.backread",      # golden BACK read (delta reconstruct)
    "install: back read XIP": "write.backread",
    "install: complete block-device": "write.complete",       # write committed (flush / no-op)
    "install: complete XIP": "write.complete",
    "install: committed FRONT": "install.committed",          # commit point passed, arming next
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
        "expect": ["boot.mount.front", "boot.ready", "log.configured", "run.clock", "run.checkin_http",
                   "run.body_read", "run.body_chunk", "run.checkin_parsed", "run.checkin_closed",
                   "run.checkin", "run.status", "run.boot_result", "run.identity",
                   "run.identity_uid", "run.offer", "run.asset_read", "run.asset_closed",
                   "run.ca_path", "run.ca_bytes", "run.read_at", "run.data_path",
                   "install.fetch_manifest",
                   "install.tls", "install.fetched", "install.manifest_ok", "install.staged",
                   "install.start", "{cov_write}", "install.download", "install.delta",
                   "install.writing", "write.ready", "write.erased", "write.wrote",
                   "write.readback", "write.backread", "write.complete", "install.committed",
                   "install.armed", "install.reboot", "boot.marked", "boot.marked_verify",
                   "verify.write", "confirm.floor", "confirm.promoted", "boot.mount_call",
                   "boot.read"],
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
        "expect": ["install.start", "install.retry", "install.retry_cleanup", "install.fallback",
                   "install.reboot", "boot.front_reject", "boot.mount.back"],
        "forbid": ["install.armed", "confirm.promoted"],
    },
    "corrupt_sha": {
        # Distinct from `corrupt`: `corrupt` flips a COMPRESSED byte so the download fails mid-
        # decompress (a bare deflate error, before the integrity check). `corrupt_sha` flips a
        # DECOMPRESSED byte + re-gzips, so the image decompresses cleanly and the failure is the
        # sha256 gate itself (install.reject_sha) -- the actual integrity trust boundary, on HW.
        # The sha256 GATE (install.reject_sha) is the point. The retry-exhaust -> golden fallback is
        # `corrupt`'s job and is SLOW here: each attempt writes the FULL image before the sha check
        # fails (~3x the N6's 12 MiB slot -> past the watch window). So assert the gate fired and the
        # bad image was retried, never committed -- the device stays on the golden VERSION throughout
        # (it never promotes 1.1.0), which satisfies end="golden".
        "desc": "image decompresses but sha256 mismatches -> integrity gate rejects the update",
        "publish": "corrupt_sha", "app": "confirm", "end": "golden",
        "expect": ["install.start", "install.reject_sha", "install.retry"],
        "forbid": ["install.armed", "install.committed", "confirm.promoted"],
    },
    "rollback": {
        "desc": "trial never confirms -> next boot rejects FRONT -> golden BACK",
        "publish": "delta", "app": "no_confirm", "end": "golden",
        "expect": ["install.armed", "boot.front_reject", "boot.mount.back"],
        "forbid": ["confirm.promoted"],
    },
    "bad_sig": {
        "desc": "manifest signature does not verify -> refused pre-erase, stays golden",
        "publish": "bad_sig", "app": "confirm", "end": "golden",
        # run.poll_tail + run.wdt_feed live AFTER run()'s install() call: a pre-erase REJECT raises
        # out of install() (no reboot), so run() reaches the loop tail and feeds the watchdog -- the
        # happy paths reboot at install() before the tail, so this is where those two are witnessed.
        "expect": ["run.offer", "install.reject", "install.reject_sig",
                   "run.poll_tail", "run.wdt_feed"],
        "forbid": ["install.start", "install.armed", "confirm.promoted", "boot.mount.back"],
    },
    "bad_key": {
        "desc": "manifest signed by a key not in the trusted allowlist -> refused pre-erase",
        # Distinct trust gate from bad_sig: bad_sig is a VALID trusted key with a broken
        # signature (crypto verify fails); bad_key is a key the device never trusted (allowlist
        # miss) -- an attacker signing with their own key. Both must reject pre-erase.
        "publish": "bad_key", "app": "confirm", "end": "golden",
        "expect": ["run.offer", "install.reject", "install.reject_key"],
        "forbid": ["install.start", "install.armed", "confirm.promoted", "boot.mount.back"],
    },
    "bad_version": {
        "desc": "version <= anti-rollback floor -> device refuses pre-erase, stays golden",
        # A full image (not a delta): a delta must go golden->newer, but here the release is
        # OLDER than golden -- the device rejects it at the version check, before rep selection.
        "publish": "full", "app": "confirm", "end": "golden", "version": "0.9.0",
        "expect": ["run.offer", "install.reject", "install.reject_vet"],
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
        # boot.no_prior_mount: the post-brick boot has both slots blank, so mp_init can't
        # auto-mount /rom -> boot.py's umount("/rom") raises -> the no-prior-mount path runs.
        "expect": ["boot.no_slot", "boot.no_prior_mount"],
        # NOT boot.mount.front: entering the SBL via machine.bootloader() boots the (still-valid)
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
        "expect": ["boot.mount.front", "boot.ready", "run.checkin", "partition.compare",
                   "sync.applying", "partition.prepare", "partition.write", "sync.applied"],
        "forbid": ["install.start", "install.armed", "sync.skip"],
    },
    "coproc_skip": {
        "desc": "coprocessor romfs already applied -> sync() is an idempotent no-op",
        # RUN AFTER coproc (partition 1 now holds the coproc romfs). sync() stream-compares,
        # finds it matches the bundle, and skips -- proving idempotence: no needless erase/write
        # of the helper-core partition on every boot.
        "publish": "none", "app": "confirm", "end": "golden",
        "expect": ["boot.mount.front", "run.checkin", "partition.compare", "sync.skip"],
        "forbid": ["sync.applying", "partition.prepare", "partition.write", "sync.applied"],
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
    scs = ["delta", "full", "rollback", "corrupt", "corrupt_sha", "bad_sig", "bad_key", "bad_version"]
    # The deep-sleep-safe watchdog runs on every OTA board: the happy path (an armed WDT survives a
    # full OTA cycle -> promoted) on all of them, so every device PR proves the on-watchdog install
    # path. The negative path (the WDT actually BITES when feeding stops, then recovers as a single
    # bite) is WWDG-specific (reset_cause==3), so watchdog_bite stays N6-only -- like no_slot is
    # block-device-only.
    scs.append("watchdog")
    if board == "OPENMV_N6":
        scs.append("watchdog_bite")
    if BOARDS[board]["flash"] == "blhost_imx":          # no_slot bricks via blhost slot-erase
        scs.append("no_slot")
    return scs


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
        "async def main():\n"
        "    _blog.info('app: main() started')\n"
        "    openmv_ota.sync()  # apply bundled coprocessor resources early (no-op if none)\n"
        "    " + bring_up +
        "    _blog.info('app: network up, starting run()')\n"
        "    asyncio.create_task(openmv_ota.run(%r, ca=%r, poll_after_s=5))\n" % (
            CFG["server"], CFG["ca_board"]) +
        trial_policy + "\n\n"
        "try:\n"
        "    import machine as _m\n"
        "    _blog.info('app: booting %s reset_cause=%d' % (openmv_ota.status().get('version'), _m.reset_cause()))\n"
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
    # A prior scenario may have left the AE3 stuck in DFU (no CDC); recover BEFORE the first
    # device op below, since these run ahead of flash_golden's own _ensure_cdc.
    _ensure_cdc(board)
    _flash_bench_files(board)


def _flash_bench_files(board, _recovered=False):
    """Push the bench CA + enable the coverage UART. Both live on /flash (survive the OTA): the CA
    for run()'s TLS, and .hilcov_uart to switch logging onto the coverage UART.

    A CANCELLED prior run can leave the mimxrt's /flash (FAT) corrupt or full, so these writes fail
    -- the classic 'RESULT: FAIL at 3s' before golden is even reflashed. On an imx board, recover
    ONCE via the CLI `flash erase`: it wipes just the user disk's MBR sector (blhost in the resident
    SBL, config-register-free) and the firmware reformats a clean FAT on the next boot. A runtime
    VfsFat.mkfs would crash the XIP-from-NOR mimxrt, so the SBL-side erase is the safe path. This
    keeps bench contention (two runs colliding) from wedging the RT until a human power-cycles it."""
    # the CA must be on the board for run()'s TLS. Push it so the harness doesn't assume a
    # hand-placed cert (tolerant: a corrupt /flash surfaces on the .hilcov_uart write below).
    if os.path.exists(CFG["ca_node"]):
        _mpremote(["fs", "cp", CFG["ca_node"], ":" + CFG["ca_board"]], timeout=30, check=False)
    try:
        # enable the coverage UART on the board (bench-only file; survives across the OTA)
        device_exec("f=open(%r,'w');f.write('%d');f.close()" % (CFG["ca_board"].rsplit("/", 1)[0] +
                    "/.hilcov_uart", BOARDS[board]["cov_uart"]))
    except Exception as e:
        if _recovered or BOARDS[board]["flash"] != "blhost_imx":
            raise                            # non-imx, or already tried recovering -- give up loud
        log("prepare: /flash write failed (%s) -> recover via `flash erase` (disk-MBR reformat)" % e)
        sh([ota("openmv-ota"), "flash", "erase", CFG["project"], "-b", board,
            "--sdk-home", CFG["sdk"], "--mpremote", ota("mpremote")], timeout=180)
        time.sleep(8)                        # let the board boot + auto-reformat the blank FAT
        _ensure_cdc(board)
        _flash_bench_files(board, _recovered=True)   # retry on the freshly reformatted /flash


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
    if bad_romfs:
        raise RuntimeError("no_slot (bad_romfs) flash not implemented for %s yet" % board)
    _ensure_cdc(board)                       # recover a wedged board so the CLI can enter DFU cleanly
    log("flash factory -> %s (openmv-ota, DFU -w)" % board)
    sh([ota("openmv-ota"), "flash", "factory", CFG["project"], "-b", board, "--sdk-home", CFG["sdk"],
        "--dfu-util", CFG["dfu"], "--mpremote", ota("mpremote")], timeout=1500)
    time.sleep(15)                           # Alif/STM32N6 take a beat to boot + re-enumerate
    _ensure_cdc(board)                       # if leave-DFU didn't re-enumerate, SWD-reset it back


def _flash_arduino_cli(board, bad_romfs=False):
    """Golden flash for the Arduino boards (Nicla Vision, Portenta, Giga) via the openmv-ota CLI's
    `flash factory`. The arduino backend enters MCUboot DFU with an automatic 1200-baud touch, then
    writes firmware + romfs (+ the CYW4343 wifi blobs) with address-based `dfu-util -w`. Unlike the
    DFU boards, the CLI's arduino factory resolves the romfs partition as <board>-romfs.img, so stage
    the dual-slot factory image under that name first (build_golden's `build factory-romfs` emits
    <board>-factory-romfs.img; the wifi blobs are already dropped into build/ by `build firmware`).
    Same rename the mimxrt path does; J-Link stays ONLY for _ensure_cdc recovery, never flashing."""
    if bad_romfs:
        raise RuntimeError("no_slot (bad_romfs) flash not implemented for %s yet" % board)
    build = CFG["project"] + "/build"
    sh("cp -f %s/%s-factory-romfs.img %s/%s-romfs.img" % (build, board, build, board))
    _ensure_cdc(board)                       # recover a wedged board so the CLI can enter DFU cleanly
    log("flash factory -> %s (openmv-ota, arduino MCUboot DFU -w)" % board)
    sh([ota("openmv-ota"), "flash", "factory", CFG["project"], "-b", board, "--sdk-home", CFG["sdk"],
        "--dfu-util", CFG["dfu"], "--mpremote", ota("mpremote")], timeout=1500)
    time.sleep(15)                           # let it boot + re-enumerate after leave-DFU
    _ensure_cdc(board)                       # if leave-DFU didn't re-enumerate, SWD-reset it back


def _dfu_leave(board):
    """Boot a board sitting in its DFU/MCUboot bootloader back into firmware WITHOUT the J-Link -- a
    dfu-util DFU-detach, the same "leave" the OpenMV IDE issues (settings.json -s :leave). Recovers a
    board stuck in DFU (an interrupted flash, or a manual/board-default bootloader entry) when its SWD
    is unavailable -- the Nicla's SWD pads are tiny and easy to lose contact on. Returns True iff a DFU
    device was present and detached; a no-op (False) otherwise, so the caller falls through to J-Link.
    Only reached when the CDC is already missing, so it never disturbs a running board."""
    rc, out = sh([CFG["dfu"], "-l"], check=False, timeout=20, quiet=True)
    if "Found DFU" not in (out or ""):
        return False                             # not in DFU -> nothing to leave; try the J-Link
    log("recover: %s is in DFU -- dfu-util detach/leave (boots firmware, no J-Link)" % board)
    sh([CFG["dfu"], "-e"], check=False, timeout=30, quiet=True)   # DFU_DETACH -> leave DFU, boot app
    time.sleep(6)                                # let it leave DFU + re-enumerate its CDC
    return True


def _ensure_cdc(board):
    """Recover a board that has wedged off USB (no CDC) via a DFU leave (if it's in its bootloader)
    then a J-Link SWD reset, so a later scenario
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
        log("recover: %s CDC missing/unresponsive at %s -- free holders, DFU-leave, J-Link SWD reset (try %d)"
            % (board, CFG["acm"], attempt + 1))
        sh("fuser -k %s 2>/dev/null || true" % CFG["acm"], check=False, quiet=True)  # any host holder
        # A board sitting in its DFU bootloader has no CDC; leave DFU first -- it boots firmware and
        # brings the CDC back with NO J-Link, which the Nicla's flaky SWD pads need. Only if that
        # doesn't restore a responsive CDC do we fall through to the SWD nRST pulse below.
        if _dfu_leave(board):
            rc2, _ = sh([ota("mpremote"), "connect", CFG["acm"], "eval", "True"],
                        timeout=15, check=False, quiet=True)
            if rc2 == 0:
                log("recover: %s CDC back after DFU leave (no J-Link)" % board)
                return
        fd, sp = tempfile.mkstemp(suffix=".jlink", prefix="recover-")
        # Two-stage recover: (1) pulse the physical nRST line (hold low, release) -- a pure pin toggle
        # that needs no core connect, so it reaches a HUNG core a debug reset can't; THEN (2) connect +
        # reset + GO. The pin pulse alone can leave the core halted/not-running (observed on the N6: it
        # reset but never re-enumerated USB until an explicit `g`), so the debug-core `r; g` actually
        # RUNS the firmware. If the core is truly hung the connect fails harmlessly -- the pulse already
        # reset it. Belt (pin, for hung) + suspenders (connect+go, for halted-but-alive).
        os.write(fd, b"si SWD\nspeed 4000\nSetRESET\nSleep 250\nClrRESET\nSleep 200\nconnect\nr\ng\nqc\n")
        os.close(fd)
        try:
            # -AutoConnect 0: do NOT try to attach the (possibly hung) core on launch -- the pin pulse
            # is physical and must not be gated on a core connect that would hang on a wedged board.
            sh([CFG["jlink"], "-device", BOARDS[board]["jlink_device"], "-if", "SWD",
                "-speed", "4000", "-AutoConnect", "0", "-CommanderScript", sp],
               timeout=60, check=False, quiet=True)
        finally:
            os.unlink(sp)
        time.sleep(8)


def _flash_blhost_imx(board, bad_romfs=False):
    """Provision golden on the mimxrt (RT1062) via the openmv-ota CLI's resident-SBL flash path
    (`flash firmware` + `flash romfs`): the CLI enters the resident SBL with machine.bootloader()
    (no jumper) and, running post-FCB, needs no FlexSPI config -- see openmv_ota.flash.imx. This
    replaces the harness's hand-rolled blhost sequence: the everyday golden flash now goes through
    the same tooling users ship with.

    bad_romfs=True is the no_slot brick: blank the whole OTA romfs region (both slots -> no valid
    trailer -> boot.py finds nothing bootable), leaving firmware + /flash intact -- via the CLI's
    `flash erase --romfs` (which enters the resident SBL and flash-erase-regions the romfs)."""
    build = CFG["project"] + "/build"
    if bad_romfs:
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
        sh("rm -f %s/build/%s-ota.delta.gz" % (CFG["project"], board), check=False)
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
        import struct
        import binascii
        target = "%s/manifests/%s/manifest.bin" % (root, rel)
        with open(target, "r+b") as f:
            data = bytearray(f.read())
        hdr = "<4sIIIIi"                                 # magic, hver, body_size, sig_size, key, alg
        hsize = struct.calcsize(hdr)                     # 24; key_id field at offset 16
        _, _, body_size, sig_size, _, _ = struct.unpack_from(hdr, data, 0)
        body_end = hsize + body_size + sig_size          # crc covers data[:body_end]
        off = 16 if which == "manifest_key" else hsize + body_size   # key_id vs signature region
        data[off] ^= 0xFF                                # break exactly that field, nothing else
        crc = binascii.crc32(bytes(data[:body_end])) & 0xFFFFFFFF
        struct.pack_into("<I", data, body_end, crc)      # re-seal so parse (+ key, for sig) pass
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
        fulls = [p for p in imgs if os.path.dirname(p).endswith(rel) and not p.endswith(".delta.gz")]
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
    # image: mid-stream flip -> the download decompress/sha256 fails AFTER the FRONT erase.
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
    ap.add_argument("--list-regression", action="store_true",
                    help="print (space-separated) the scenarios this board+network runs in the "
                         "full regression, then exit -- the hil-ota workflow loops over these")
    args = ap.parse_args()

    network = args.network or BOARDS[args.board]["network"]
    if args.list_regression:
        print(" ".join(regression_scenarios(args.board, network)))
        return 0
    spec = SCENARIOS[args.scenario]
    expect, forbid = scenario_markers(args.board, args.scenario)
    pub_version = spec.get("version", args.target)     # bad_version publishes below the floor
    t0 = time.time()
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
        srv = bench_server.start(ota("python"), log=log)
        CFG["server"], CFG["ca_node"], CFG["artifacts"], CFG["token"] = (
            srv["url"], srv["ca"], srv["store"], srv["token"])
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
            devid = None
            if not args.skip_provision:
                phase("prepare", lambda: prepare(args.board, args.checkout, network, spec["app"]))
                phase("build_golden", lambda: build_golden(args.board))
                phase("flash_golden", lambda: flash_golden(args.board))
                # verify_golden reads the device_id in the same early (pre-watchdog-arm) exec.
                devid = phase("verify_golden", verify_golden)
            if devid is None:                    # --skip-provision: board already up, read it directly
                devid = device_id()
            trace["device_id"] = devid
            log("device_id: " + devid)
            if args.scenario == "coproc":        # dirty partition 1 so sync() APPLIES (not skips)
                phase("dirty_coproc", dirty_coproc_partition)
            if spec["publish"] != "none" and not args.skip_publish:
                phase("publish", lambda: publish_update(args.board, pub_version, spec["publish"]))
                trace["metrics"] = artifact_sizes(args.board)   # download sizes -> ota_metrics report
            cap = UartCapture(CFG["uart"])
            cap.start(time.time())
            # "install" phase = the whole autonomous OTA (check-in -> download -> write -> trial ->
            # confirm/promote or fallback). Timing it makes install SPEED a tracked metric too.
            result = phase("install", lambda: run_cycle(
                devid, "1.0.0", args.target, spec["end"], expect, cap, args.timeout))
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
        bench_server.stop(srv)                    # tear the per-run server + store down
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

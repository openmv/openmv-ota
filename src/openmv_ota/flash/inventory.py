"""`flash list` -- enumerate connected boards and the state each is in.

Three scanners, one per transport, mapped back to board names through the very VID:PIDs
``boards.json`` already carries for flashing:

* **serial** (pyserial) -- a board running its firmware (its ``runtime`` id, or an Arduino's
  app id), or an AE3 held in SE-UART maintenance mode (its FTDI/CH340 bridge);
* **dfu** (``dfu-util -l``) -- a board in the OpenMV/Arduino DFU bootloader, or an STM32 in its
  system ROM DFU;
* **imx** (an spsdk USB scan via the SDK's python) -- an RT1060 in its ROM serial-download mode.

Every device is reported in one of three **states**: ``running`` (firmware is up), ``bootloader``
(the normal DFU we flash firmware/romfs through), or ``recovery`` -- the by-hand ROM/maintenance
modes (system DFU, SE-UART, SDP) you enter to flash a *bootloader*. The same id can belong to
several boards (every STM32 shares ``0483:df11`` in recovery), so those collapse to a generic
label.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from openmv_ota.romfs.boards import load_boards

from . import device, imx, runner

# An id shared by several boards -> the generic label to report (you can't tell which board a
# bare ST system-ROM DFU is until you flash it).
_SHARED = {(0x0483, 0xDF11): "OpenMV STM32"}

_DFU_VIDPID = re.compile(r"\[([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\]")
_DFU_SERIAL = re.compile(r'serial="([^"]*)"')


@dataclass(frozen=True)
class Device:
    board: str
    state: str           # running | bootloader | recovery
    where: str           # the serial port, or the USB mode (DFU / system DFU / system flashloader)
    serial: str | None   # USB serial number when known (what `--serial` pins)


def _records():
    """Yield ``(vid, pid, board, state, source)`` for every identifiable USB id, skipping
    boards with no flash config or that are retired."""
    for name, b in load_boards().items():
        f = b.flash
        if not f or b.unsupported:
            continue
        backend = f.get("backend")
        rt = f.get("runtime")
        if rt:
            yield (*device._ids(rt), name, "running", "serial")
        app = f.get("app")
        if app:
            vid, base = device._ids(app["usb"])
            for p in {base, *(int(x, 16) for x in app.get("pids", []))}:
                yield (vid, p, name, "running", "serial")
        usb = f.get("usb")
        if usb and backend in ("dfu", "arduino"):
            yield (*device._ids(usb), name, "bootloader", "dfu")
        bl = f.get("bootloader") or {}
        if bl.get("backend") == "dfu":                       # STM32 system ROM DFU
            yield (*device._ids(bl["usb"]), name, "recovery", "dfu")
        elif bl.get("backend") == "alif":                    # AE3 SE-UART bridges
            for v in bl["variants"]:
                yield (*device._ids(v["bridge"]), name, "recovery", "serial")


def _index() -> dict[tuple[int, int], tuple[str, str, str]]:
    """``(vid, pid) -> (label, state, source)``; shared ids collapse to their generic label."""
    idx: dict[tuple[int, int], tuple[str, str, str]] = {}
    for vid, pid, name, state, source in _records():
        key = (vid, pid)
        idx[key] = (_SHARED.get(key, name), state, source)
    return idx


def serial_devices() -> list[Device]:
    """Boards visible on the serial ports -- running firmware, or an AE3 SE-UART bridge."""
    idx = _index()
    out = []
    for p in device._comports():
        hit = idx.get((p.vid, p.pid))
        if hit and hit[2] == "serial":
            label, state, _ = hit          # the state column already says running vs recovery
            out.append(Device(label, state, p.device, p.serial_number))
    return out


def dfu_devices(dfu_util: str) -> list[Device]:
    """Boards in a DFU mode, from ``dfu-util -l`` -- the OpenMV/Arduino bootloader or an STM32
    in system ROM DFU. dfu-util prints one line per alt-setting, so collapse by id+serial."""
    idx = _index()
    out, seen = [], set()
    for line in runner.output([dfu_util, "-l"]).splitlines():
        m = _DFU_VIDPID.search(line)
        if not m:
            continue
        key = (int(m.group(1), 16), int(m.group(2), 16))
        hit = idx.get(key)
        if not hit or hit[2] != "dfu":
            continue
        sm = _DFU_SERIAL.search(line)
        serial = sm.group(1) if sm and sm.group(1) not in ("", "UNKNOWN") else None
        if (key, serial) in seen:
            continue
        seen.add((key, serial))
        label, state, _ = hit
        out.append(Device(label, state, "system DFU" if state == "recovery" else "DFU", serial))
    return out


def imx_devices(python3: str) -> list[Device]:
    """RT1060-class boards on their i.MX serial-download USB, via an spsdk scan run under
    ``python3``: the **ROM downloader** (SDP -- held in recovery, ready to flash) *and* the RAM
    **flashloader** it loads (the MCU bootloader ``blhost`` talks to). The flashloader is present
    only mid-flash, so seeing it means a flash was interrupted while the loader was up."""
    sdp, mboot = imx._SDP_IF, imx._MBOOT_IF
    entries = []                                         # (device_id, Device, module, class)
    for name, b in load_boards().items():
        f = b.flash
        if not f or b.unsupported or not f.get("sdphost"):
            continue
        entries.append((f["sdphost"]["usb"],
                        Device(name, "recovery", "system flashloader", None), *sdp))
        entries.append((f["blhost"]["usb"],
                        Device(name, "bootloader", "flashloader (mid-flash)", None), *mboot))
    out = runner.output(imx.scan_argv(python3, [(mod, cls, dev) for dev, _, mod, cls in entries]))
    found = {ln[len("FOUND "):].strip() for ln in out.splitlines() if ln.startswith("FOUND ")}
    return [dev for devid, dev, _, _ in entries if devid in found]

"""Orchestration: artifact selection, alt mapping, multi-step reset, dry-run, history."""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

import pytest

from openmv_ota.flash import flash as fl
from openmv_ota.flash.errors import FlashError
from openmv_ota.flash.targets import flash_config
from tests import built


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project dir with a build/ folder; the runner + tool + history are stubbed so the
    test asserts the argv sequence without touching hardware or the filesystem log."""
    (tmp_path / "build").mkdir()
    ran: list[list[str]] = []
    recorded: list[dict] = []
    monkeypatch.setattr(fl.runner, "run", lambda argv, **kw: ran.append(argv))
    # the bootloader-entry reset (mpremote) runs captured; it exits 1 as the USB drops, as on HW
    monkeypatch.setattr(fl.runner, "run_quiet", lambda argv: (ran.append(argv), (1, "EIO"))[1])
    monkeypatch.setattr(fl.tools, "find_dfu_util", lambda override, sdk_home: override or "DFU")
    monkeypatch.setattr(fl.history, "record",
                        lambda root, action, **f: recorded.append({"action": action, **f}))

    def artifact(name, data=b"x"):            # as `build firmware` on this machine leaves it
        (tmp_path / "build" / name).write_bytes(data)
        built.record(tmp_path, tmp_path / "build" / name)

    return tmp_path, ran, recorded, artifact


def _capture(monkeypatch, ran):
    """The fixture's runner, also keeping each file a step wrote as it was when it ran: the
    firmware files are written from a private copy, gone once the flash is over."""
    wrote = {}

    def run(argv, **kw):
        ran.append(argv)
        f = Path(argv[-1])
        if f.is_file():
            wrote[f.name] = (f, f.read_bytes())
    monkeypatch.setattr(fl.runner, "run", run)
    return wrote


def _private(path, root):
    """A file the flash wrote from its private copy: outside the project, removed after."""
    path = Path(path)
    return root not in path.parents and path.parent.name.startswith("openmv-ota-flash-") \
        and not path.parent.exists()


def test_flash_firmware(project, monkeypatch):
    root, ran, recorded, artifact = project
    artifact("OPENMV4-firmware.bin", b"built here")
    wrote = _capture(monkeypatch, ran)
    steps = fl.flash_firmware(str(root), board="OPENMV4")
    assert [s.alt for s in steps] == [2]
    # the FIRST write runs without -w: _await_dfu has already seen the device listed and settled
    assert ran[0][:-1] == ["DFU", "-d", ",37c5:9204", "-a", "2", "--reset", "-D"]
    assert _private(ran[0][-1], root) and wrote["OPENMV4-firmware.bin"][1] == b"built here"
    assert recorded == [{"action": "flash-firmware", "board": "OPENMV4",
                         "files": [{"file": "OPENMV4-firmware.bin", "alt": 2}]}]


def test_flash_factory_is_multistep_and_resets_only_last(project):
    root, ran, _rec, artifact = project
    artifact("OPENMV4-firmware.bin")
    artifact("OPENMV4-factory-romfs.img")
    steps = fl.flash_factory(str(root), board="OPENMV4")
    assert [(s.artifact, s.alt) for s in steps] == [("firmware", 2), ("romfs", 3)]
    assert "--reset" not in ran[0]                   # firmware step stays in the bootloader
    assert "--reset" in ran[1]                       # only the final write reboots
    assert "-w" not in ran[0] and "-w" in ran[1]     # later steps keep -w (the board stays in DFU)


def test_flash_romfs(project):
    root, ran, _rec, artifact = project
    artifact("OPENMV4-romfs.img")
    fl.flash_romfs(str(root), board="OPENMV4")
    assert ran[0][3:5] == ["-a", "3"] and "--reset" in ran[0]


def test_no_reset(project):
    root, ran, _rec, artifact = project
    artifact("OPENMV4-romfs.img")
    fl.flash_romfs(str(root), board="OPENMV4", reset=False)
    assert "--reset" not in ran[0]


def test_ae3_firmware_flashes_both_cores(project):
    # the HE core ships with the firmware -- both images, always, no flag
    root, ran, _rec, artifact = project
    artifact("OPENMV_AE3-stub.bin")                        # a stub board: the HP image rides behind it
    artifact("OPENMV_AE3-firmware-M55_HP.bin")
    artifact("OPENMV_AE3-firmware-M55_HE.bin")
    steps = fl.flash_firmware(str(root), board="OPENMV_AE3")
    assert [(s.alt, s.file.name) for s in steps] == [
        (1, "OPENMV_AE3-firmware-with-stub.bin"), (2, "OPENMV_AE3-firmware-M55_HE.bin")]
    assert ran[0][2] == ",37c5:96e3" and "--reset" in ran[1] and "--reset" not in ran[0]


def test_ae3_firmware_requires_both_cores(project):
    root, ran, _rec, artifact = project
    artifact("OPENMV_AE3-firmware-M55_HP.bin")            # the stub missing -> fail fast, flash nothing
    with pytest.raises(FlashError, match="OPENMV_AE3-stub.bin"):
        fl.flash_firmware(str(root), board="OPENMV_AE3")
    artifact("OPENMV_AE3-stub.bin")                       # HE missing -> the same
    with pytest.raises(FlashError, match="firmware-M55_HE.bin"):
        fl.flash_firmware(str(root), board="OPENMV_AE3")
    assert ran == []


def test_ae3_factory_flashes_all_four_partitions(project):
    root, ran, _rec, artifact = project
    for n in ("firmware-M55_HP", "firmware-M55_HE", "stub"):
        artifact("OPENMV_AE3-%s.bin" % n)
    artifact("OPENMV_AE3-coprocessor-romfs.img")
    artifact("OPENMV_AE3-factory-romfs.img")
    steps = fl.flash_factory(str(root), board="OPENMV_AE3")
    assert [(s.artifact, s.alt) for s in steps] == [
        ("firmware", 1), ("coprocessor", 2), ("coprocessor_romfs", 3), ("romfs", 6)]
    assert sum("--reset" in a for a in ran) == 1 and "--reset" in ran[-1]


def test_missing_artifact_fails_before_running(project):
    root, ran, _rec, _artifact = project
    with pytest.raises(FlashError, match="missing artifact"):
        fl.flash_firmware(str(root), board="OPENMV4")
    assert ran == []                                 # nothing flashed


def test_factory_resolves_all_before_flashing(project):
    # firmware present but the factory image isn't -> fail fast, flash nothing
    root, ran, _rec, artifact = project
    artifact("OPENMV4-firmware.bin")
    with pytest.raises(FlashError, match="factory-romfs"):
        fl.flash_factory(str(root), board="OPENMV4")
    assert ran == []


def test_dry_run_records_nothing_and_runs_nothing(project):
    root, ran, recorded, artifact = project
    artifact("OPENMV4-firmware.bin")
    steps = fl.flash_firmware(str(root), board="OPENMV4", dry_run=True)
    assert ran == [] and recorded == []
    assert "--reset" in steps[0].argv             # argv still built for display


def test_custom_output_dir(project, tmp_path):
    root, ran, _rec, _artifact = project
    out = tmp_path / "dist"
    out.mkdir()
    (out / "OPENMV4-romfs.img").write_bytes(b"x")
    fl.flash_romfs(str(root), board="OPENMV4", output=str(out))
    assert str(out / "OPENMV4-romfs.img") in ran[0]


def test_single_core_board_flashes_only_firmware(project):
    # OPENMV4 has no coprocessor target, so firmware is a single image
    root, ran, _rec, artifact = project
    artifact("OPENMV4-firmware.bin")
    steps = fl.flash_firmware(str(root), board="OPENMV4")
    assert [s.artifact for s in steps] == ["firmware"]


def test_resolve_dfu_util_dry_run_tolerates_missing_dfu_util(monkeypatch):
    monkeypatch.setattr(fl.tools, "find_dfu_util",
                        lambda override, sdk_home: (_ for _ in ()).throw(FlashError("nope")))
    assert fl._resolve_dfu_util(None, None, dry_run=True) == "dfu-util"
    assert fl._resolve_dfu_util("/x/dfu-util", None, dry_run=True) == "/x/dfu-util"
    with pytest.raises(FlashError):
        fl._resolve_dfu_util(None, None, dry_run=False)


# --- imx (RT1060) -------------------------------------------------------------------------

@pytest.fixture
def imx_project(tmp_path, monkeypatch):
    """An RT1060 project with build artifacts + flashloaders; runner/tools stubbed."""
    (tmp_path / "build").mkdir()
    ran: list[list[str]] = []
    recorded: list[dict] = []
    monkeypatch.setattr(fl.runner, "run", lambda argv, **kw: ran.append(argv))
    monkeypatch.setattr(fl.tools, "find_spsdk", lambda name, sdk_home: name.upper())
    # the resident-SBL catcher/reset is a hardware step (Popen spsdk + machine.bootloader); the
    # plan/step tests only care about the blhost argv sequence, so stub it out.
    monkeypatch.setattr(fl, "_imx_catch_and_reset", lambda *a, **k: None)
    monkeypatch.setattr(fl.history, "record",
                        lambda root, action, **f: recorded.append({"action": action, **f}))
    # the flashloaders are bundled in the package; only the build artifacts go in build/
    for n in ("OPENMV_RT1060-firmware.bin", "OPENMV_RT1060-romfs.img",
              "OPENMV_RT1060-factory-romfs.img", "OPENMV_RT1060-stub.bin"):
        (tmp_path / "build" / n).write_bytes(b"x" * 5000)
        built.record(tmp_path, tmp_path / "build" / n)
    return tmp_path, ran, recorded


def test_imx_firmware_runs_the_sequence(imx_project):
    root, ran, recorded = imx_project
    steps = fl.flash_firmware(str(root), board="OPENMV_RT1060")
    # resident-SBL path (catcher entry is stubbed in the fixture): NO sdphost, NO FlexSPI config, and
    # NO wait step in the plan -- just erase+write firmware, then reset.
    assert not any(a[0] == "SDPHOST" for a in ran)
    assert not any("fill-memory" in a or "configure-memory" in a for a in ran)
    assert "flash-erase-region" in ran[0] and "0x60040000" in ran[0]
    assert ran[-1][-1] == "reset"
    # a stub board: the composed stub || pad || firmware image is what lands at the firmware address
    [w] = [a for a in ran if "write-memory" in a and "0x60040000" in a]
    assert w[-1].endswith("OPENMV_RT1060-firmware-with-stub.bin") and _private(w[-1], root)
    fl.flash_firmware(str(root), board="OPENMV_RT1060", dry_run=True)   # composed in build/
    assert (root / "build" / "OPENMV_RT1060-firmware-with-stub.bin").stat().st_size == 0x10000 + 5000
    assert recorded[0]["action"] == "flash-firmware" and recorded[0]["steps"] == \
        [s.label for s in steps]


def test_imx_provision_is_the_full_rom_path(imx_project, capsys):
    root, ran, _rec = imx_project
    fl.flash_factory(str(root), board="OPENMV_RT1060", provision=True)
    flat = " ".join(" ".join(a) for a in ran)
    assert "efuse-program-once 0x06 00000010" in flat and "0x60001000" in flat
    assert ran[0][0] == "SDPHOST" or "SdpUSBInterface" in flat     # over the ROM's serial download
    assert "SBL to 3.3V" in capsys.readouterr().err                 # says how to get there


def test_imx_factory_goes_through_the_resident_sbl(imx_project, monkeypatch):
    """A customer RT1062 already has its SBL: `flash factory` must work with a camera plugged in
    over USB -- firmware + the factory romfs through machine.bootloader() -> blhost, and NONE of
    the blank-board provisioning (no ROM/SDP wait, no FCB, no SBL rewrite, no e-fuse)."""
    root, ran, recorded = imx_project
    entered = []
    monkeypatch.setattr(fl, "_imx_catch_and_reset", lambda *a, **k: entered.append(a))
    fl.flash_factory(str(root), board="OPENMV_RT1060")
    flat = " ".join(" ".join(a) for a in ran)
    assert entered, "the resident SBL is entered (and claimed) first"
    assert not any(a[0] == "SDPHOST" for a in ran) and "SdpUSBInterface" not in flat
    for gone in ("efuse", "0x60000000", "0x60001000", "fill-memory", "configure-memory"):
        assert gone not in flat, gone
    writes = [a for a in ran if "write-memory" in a]
    assert [w[-2] for w in writes] == ["0x60040000", "0x60800000"]          # firmware, romfs
    assert writes[0][-1].endswith("OPENMV_RT1060-firmware-with-stub.bin")  # stub || pad || firmware
    assert writes[1][-1].endswith("OPENMV_RT1060-factory-romfs.img")      # the dual-slot image
    assert ran[-1][-1] == "reset" and recorded[0]["action"] == "flash-factory"


def test_provision_is_refused_off_imx(project):
    root, ran, _rec, artifact = project
    with pytest.raises(FlashError, match="i.MX ROM provisioning"):
        fl.flash_factory(str(root), board="OPENMV4", provision=True)
    assert ran == []


def test_imx_dry_run_runs_nothing(imx_project):
    root, ran, recorded = imx_project
    steps = fl.flash_romfs(str(root), board="OPENMV_RT1060", dry_run=True)
    assert ran == [] and recorded == []
    assert steps[-1].argv[-1] == "reset"


def test_imx_sdk_python_is_beside_blhost():
    assert fl._sdk_python("/opt/sdk/python/bin/blhost") == "/opt/sdk/python/bin/python3"


def test_imx_a_python_older_than_3_11_beside_blhost_is_refused_before_a_helper_runs(
        imx_project, tmp_path, monkeypatch):
    """The spsdk helpers run with -P, which Python has from 3.11. A blhost on PATH beside an
    older python3 failed every helper on the flag, and the first said only that the catcher
    never armed."""
    root, ran, _recorded = imx_project
    bin_ = tmp_path / "venv" / "bin"
    bin_.mkdir(parents=True)
    (bin_ / "python3.10").write_text("")
    (bin_ / "python3").symlink_to("python3.10")
    monkeypatch.setattr(fl.tools, "find_spsdk", lambda name, sdk_home: str(bin_ / name))
    with pytest.raises(FlashError, match="Python 3.10, and the spsdk helpers need 3.11"):
        fl.flash_romfs(str(root), board="OPENMV_RT1060")
    assert ran == []
    (bin_ / "python3").unlink()
    (bin_ / "python3.12").write_text("")
    (bin_ / "python3").symlink_to("python3.12")
    assert fl._sdk_python(str(bin_ / "blhost")) == str(bin_ / "python3")


def test_imx_uses_bundled_flashloader(imx_project):
    # the RAM flashloaders are an internal crutch shipped in the package -- never the user's -- and
    # only the RECOVERY path (factory/bootloader, over SDP) loads one; the everyday firmware/romfs
    # path drives the resident SBL and needs none.
    root, ran, _rec = imx_project
    fl.flash_factory(str(root), board="OPENMV_RT1060", provision=True)
    assert any("data/flashloaders/OPENMV_RT1060/sdphost_flash_loader.bin" in a[-1] for a in ran)
    n = len(ran)
    fl.flash_firmware(str(root), board="OPENMV_RT1060")
    assert not any("flash_loader.bin" in a[-1] for a in ran[n:])   # firmware path loads no flashloader


def test_imx_missing_build_artifact_errors(tmp_path, monkeypatch):
    # loaders are bundled, but the firmware image still has to be built first
    (tmp_path / "build").mkdir()
    monkeypatch.setattr(fl.tools, "find_spsdk", lambda name, sdk_home: name)
    with pytest.raises(FlashError, match="OPENMV_RT1060-stub.bin -- `build firmware` produces it"):
        fl.flash_firmware(str(tmp_path), board="OPENMV_RT1060")      # a stub board: the stub first
    (tmp_path / "build" / "OPENMV_RT1060-stub.bin").write_bytes(b"s" * 100)
    with pytest.raises(FlashError, match="OPENMV_RT1060-firmware.bin"):
        fl.flash_firmware(str(tmp_path), board="OPENMV_RT1060")
    (tmp_path / "build" / "OPENMV_RT1060-firmware.bin").write_bytes(b"f" * 100)
    built.record(tmp_path, *(tmp_path / "build").iterdir())
    with pytest.raises(FlashError, match="OPENMV_RT1060-factory-romfs.img"):
        fl.flash_factory(str(tmp_path), board="OPENMV_RT1060")


# --- resident-SBL catcher orchestration (the IDE's imxArmCatcher, ported) -------------------

def _proc(code):
    """A real short-lived process running `code` with a piped stdout (so _await_line's select +
    readline hit the real code path, not a mock)."""
    import subprocess
    return subprocess.Popen([sys.executable, "-c", code],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def test_await_line_finds_marker_then_next_marker():
    p = _proc("import time,sys\nprint('READY',flush=True)\ntime.sleep(0.1)\n"
              "print('CLAIMED 1',flush=True)\n")
    try:
        assert fl._await_line(p, "READY", 5) is True       # first marker
        assert fl._await_line(p, "CLAIMED", 5) is True      # a later marker on the same stream
    finally:
        p.wait()


def test_await_line_clean_exit_without_marker_is_true():
    p = _proc("pass")                                       # exits 0, prints nothing
    assert fl._await_line(p, "NOPE", 5) is True             # EOF/exit-0 path


def test_await_line_times_out_on_a_silent_process():
    p = _proc("import time; time.sleep(30)")                # alive, never prints
    try:
        assert fl._await_line(p, "X", 0.5) is False         # timeout path
    finally:
        p.terminate()
        p.wait()


class _FakeCatcher:
    def __init__(self, *a, alive=False, **k):
        self.stdout = None
        self.terminated = False
        self._alive = alive                                 # alive -> the finally must terminate it

    def poll(self):
        return None if self._alive else 0                   # a successful claim exits the catcher

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


def _patch_catcher(monkeypatch, popens, *, ready=True, claimed=True, alive=False):
    import subprocess
    made = []

    def fake_popen(argv, **k):
        popens.append(argv)
        c = _FakeCatcher(alive=alive)
        made.append(c)
        return c
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    awaited = iter([ready, claimed])
    monkeypatch.setattr(fl, "_await_line", lambda p, m, t: next(awaited))
    monkeypatch.setattr(fl, "_mpremote", lambda o: ["mpremote"])
    return made


def test_imx_catch_and_reset_arms_resets_and_claims(monkeypatch):
    popens = []
    _patch_catcher(monkeypatch, popens)
    monkeypatch.setattr(fl.device, "select", lambda raw, serial: fl.device.Camera("/dev/ttyACM0", "SN"))
    fl._imx_catch_and_reset({"blhost": {"usb": "0x15A2,0x0073"}}, "python3", None, None)
    assert any("claim" in a for a in popens)                # armed the catcher (claim mode)
    # Reset the running camera into the SBL with `exec machine.bootloader()`, NOT mpremote's
    # `bootloader` subcommand -- the distinction device.reset() documents for the DFU boards. This
    # test previously asserted the subcommand and so pinned it in place.
    reset = [a for a in popens if "connect" in a]
    assert reset, "expected the running camera to be reset into the SBL"
    assert "exec" in reset[0] and "machine.bootloader()" in " ".join(reset[0])
    assert "bootloader" not in reset[0], "the bare subcommand must not be used"


def test_imx_catch_and_reset_no_camera_skips_reset(monkeypatch):
    popens = []
    _patch_catcher(monkeypatch, popens)
    monkeypatch.setattr(fl.device, "select", lambda raw, serial: None)   # already in the SBL
    fl._imx_catch_and_reset({"blhost": {"usb": "0x15A2,0x0073"}}, "python3", None, None)
    assert any("claim" in a for a in popens)
    assert not any("bootloader" in a for a in popens)       # nothing running to reset


def test_imx_catch_and_reset_raises_when_never_armed(monkeypatch):
    _patch_catcher(monkeypatch, [], ready=False)
    with pytest.raises(FlashError, match="never armed"):
        fl._imx_catch_and_reset({"blhost": {"usb": "0x15A2,0x0073"}}, "python3", None, None)


def test_imx_catch_and_reset_raises_when_sbl_not_claimed_and_terminates_catcher(monkeypatch):
    monkeypatch.setattr(fl.device, "select", lambda raw, serial: None)
    made = _patch_catcher(monkeypatch, [], claimed=False, alive=True)   # catcher still running on fail
    with pytest.raises(FlashError, match="could not be claimed"):
        fl._imx_catch_and_reset({"blhost": {"usb": "0x15A2,0x0073"}}, "python3", None, None)
    assert made[0].terminated                                           # finally cleaned it up


def test_imx_catch_announces_armed_then_claimed(monkeypatch, capsys):
    popens = []
    _patch_catcher(monkeypatch, popens)
    monkeypatch.setattr(fl.device, "select", lambda raw, serial: None)
    fl._imx_catch_and_reset({"blhost": {"usb": "0x15A2,0x0073"}}, "python3", None, None)
    err = capsys.readouterr().err
    assert fl.IMX_ARMED in err and fl.IMX_CLAIMED in err
    assert err.index(fl.IMX_ARMED) < err.index(fl.IMX_CLAIMED)
    assert popens[0][popens[0].index("claim") + 2] == "%g" % fl._IMX_RESET_WAIT_S


def test_imx_in_bootloader_waits_bounded_and_never_touches_the_cdc(monkeypatch, capsys):
    """--in-bootloader: the SBL is up or about to be. No device.select, no machine.bootloader --
    just a catcher armed for the longer bounded wait, announced so an outside trigger can fire."""
    popens, waits = [], []
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **k: popens.append(argv) or _FakeCatcher())
    monkeypatch.setattr(fl, "_await_line", lambda p, m, t: waits.append((m, t)) or True)

    def boom(*a, **k):
        raise AssertionError("--in-bootloader must not look for a running camera")
    monkeypatch.setattr(fl.device, "select", boom)
    fl._imx_catch_and_reset({"blhost": {"usb": "0x15A2,0x0073"}}, "python3", None, None,
                            enter_bootloader=False)
    assert len(popens) == 1 and "claim" in popens[0]                     # the catcher only
    assert popens[0][popens[0].index("claim") + 2] == "%g" % fl.IMX_IN_BOOTLOADER_WAIT_S
    assert waits[1] == ("CLAIMED", fl.IMX_IN_BOOTLOADER_WAIT_S + 15)
    assert fl.IMX_ARMED in capsys.readouterr().err


@pytest.mark.parametrize("enter, cam, why", [
    (False, None, "--in-bootloader: nothing brought the SBL up"),
    (True, None, "no running camera was found"),
    (True, "cam", "machine.bootloader() was sent to /dev/ttyACM0"),
])
def test_imx_claim_failure_says_why(monkeypatch, enter, cam, why):
    _patch_catcher(monkeypatch, [], claimed=False)
    camera = fl.device.Camera("/dev/ttyACM0", "SN") if cam else None
    monkeypatch.setattr(fl.device, "select", lambda raw, serial: camera)
    with pytest.raises(FlashError, match="could not be claimed") as e:
        fl._imx_catch_and_reset({"blhost": {"usb": "0x15A2,0x0073"}}, "python3", None, None,
                                enter_bootloader=enter)
    assert why in str(e.value)


def test_imx_flash_passes_in_bootloader_to_the_catcher(imx_project, monkeypatch):
    root, ran, _rec = imx_project
    seen = []
    monkeypatch.setattr(fl, "_imx_catch_and_reset", lambda *a, **k: seen.append(a))
    fl.flash_factory(str(root), board="OPENMV_RT1060", enter_bootloader=False)
    assert seen[0][-1] is False
    assert any("write-memory" in a and "0x60800000" in a for a in ran)


def test_prepare_skips_reset_for_imx(monkeypatch):
    # imx: _prepare must NOT reset (the catcher arms before the reset) -- it just resolves the serial
    monkeypatch.setattr(fl.device, "select", lambda raw, serial: fl.device.Camera("/dev/ttyACM0", "SN9"))
    monkeypatch.setattr(fl.device, "reset", lambda *a, **k: pytest.fail("imx must not reset in _prepare"))
    raw = flash_config("OPENMV_RT1060").raw
    assert fl._prepare(raw, serial=None, enter_bootloader=True, mpremote=None, dry_run=False) == "SN9"




def test_resolve_spsdk_dry_run_tolerates_missing(monkeypatch):
    monkeypatch.setattr(fl.tools, "find_spsdk",
                        lambda name, sdk_home: (_ for _ in ()).throw(FlashError("nope")))
    assert fl._resolve_spsdk("blhost", None, dry_run=True) == "blhost"
    with pytest.raises(FlashError):
        fl._resolve_spsdk("blhost", None, dry_run=False)


# --- arduino (Portenta / Giga / Nicla) ----------------------------------------------------

@pytest.fixture
def arduino_project(tmp_path, monkeypatch):
    """An Arduino project; runner/dfu-util stubbed, no camera attached (conftest). The wifi
    blobs sit in the output dir (build emits them there), with the firmware/romfs artifacts."""
    (tmp_path / "build").mkdir()
    ran: list[list[str]] = []
    monkeypatch.setattr(fl.runner, "run", lambda argv, **kw: ran.append(argv))
    monkeypatch.setattr(fl.tools, "find_dfu_util", lambda override, sdk_home: override or "DFU")
    monkeypatch.setattr(fl.history, "record", lambda *a, **k: None)
    for n in ("ARDUINO_PORTENTA_H7-firmware.bin", "ARDUINO_PORTENTA_H7-romfs.img",
              "ARDUINO_PORTENTA_H7-factory-romfs.img", "ARDUINO_PORTENTA_H7-stub.bin",
              "ARDUINO_PORTENTA_H7-firmware-text2.bin",
              "cyw4343_7_45_98_102.bin", "cyw4343_btfw.bin"):
        (tmp_path / "build" / n).write_bytes(b"x")
        built.record(tmp_path, tmp_path / "build" / n)
    return tmp_path, ran


def test_arduino_firmware(arduino_project):
    """A stub board: the second text region to its sector first, then the composed
    stub || pad || firmware image at the address MCUboot jumps to (never the bare firmware,
    which is linked to run behind the stub), and only the last write leaves DFU."""
    root, ran = arduino_project
    fl.flash_firmware(str(root), board="ARDUINO_PORTENTA_H7")
    assert [a[a.index("-s") + 1] for a in ran] == ["0x08020000", "0x08040000:leave"]
    assert "-w" not in ran[0] and "-w" in ran[1]              # later writes wait for the device
    assert Path(ran[0][-1]).name == "ARDUINO_PORTENTA_H7-firmware-text2.bin"
    assert Path(ran[1][-1]).name == "ARDUINO_PORTENTA_H7-firmware-with-stub.bin"
    assert _private(ran[0][-1], root) and _private(ran[1][-1], root)


def test_arduino_firmware_skips_an_empty_second_region_and_needs_the_stub(arduino_project):
    root, ran = arduino_project
    (root / "build/ARDUINO_PORTENTA_H7-firmware-text2.bin").write_bytes(b"")   # nothing moved
    fl.flash_firmware(str(root), board="ARDUINO_PORTENTA_H7")
    assert [a[a.index("-s") + 1] for a in ran] == ["0x08040000:leave"]
    (root / "build/ARDUINO_PORTENTA_H7-firmware-text2.bin").unlink()
    with pytest.raises(FlashError, match="firmware-text2.bin -- build it first"):
        fl.flash_firmware(str(root), board="ARDUINO_PORTENTA_H7", dry_run=True)
    (root / "build/ARDUINO_PORTENTA_H7-stub.bin").unlink()
    with pytest.raises(FlashError, match="ARDUINO_PORTENTA_H7-stub.bin -- `build firmware` produces it"):
        fl.flash_firmware(str(root), board="ARDUINO_PORTENTA_H7", dry_run=True)


def test_arduino_factory_writes_wifi_from_output_dir(arduino_project):
    root, ran = arduino_project
    fl.flash_factory(str(root), board="ARDUINO_PORTENTA_H7")
    assert [a[a.index("-s") + 1] for a in ran] == [
        "0x90F00000", "0x90FC0000", "0x08020000", "0x08040000", "0x90B00000:leave"]
    assert Path(ran[0][-1]).name == "cyw4343_7_45_98_102.bin"   # from the build outputs, as checked
    assert Path(ran[3][-1]).name == "ARDUINO_PORTENTA_H7-firmware-with-stub.bin"
    assert _private(ran[0][-1], root) and _private(ran[3][-1], root)
    # the dual-slot image `build factory-romfs` writes, not the single-slot app romfs
    assert ran[-1][-1] == str(root / "build/ARDUINO_PORTENTA_H7-factory-romfs.img")


def test_arduino_dry_run_runs_nothing(arduino_project):
    root, ran = arduino_project
    steps = fl.flash_romfs(str(root), board="ARDUINO_PORTENTA_H7", dry_run=True)
    assert ran == [] and steps[0].argv[6] == "0x90B00000:leave"


def test_arduino_missing_artifact_errors(tmp_path, monkeypatch):
    (tmp_path / "build").mkdir()
    monkeypatch.setattr(fl.tools, "find_dfu_util", lambda override, sdk_home: "DFU")
    with pytest.raises(FlashError, match="ARDUINO_PORTENTA_H7-stub.bin"):    # a stub board: checked first
        fl.flash_firmware(str(tmp_path), board="ARDUINO_PORTENTA_H7", dry_run=True)
    with pytest.raises(FlashError, match="ARDUINO_PORTENTA_H7-romfs.img -- build it first"):
        fl.flash_romfs(str(tmp_path), board="ARDUINO_PORTENTA_H7", dry_run=True)


# --- device prepare (detect running camera, reset into bootloader, pin -S serial) ---------

class _Port:
    def __init__(self, vid, pid, dev, serial=None):
        self.vid, self.pid, self.device, self.serial_number = vid, pid, dev, serial


def _running(monkeypatch, *cams):
    monkeypatch.setattr(fl.device, "_comports", lambda: list(cams))


def test_mpremote_default_and_override():
    assert fl._mpremote(None)[1:] == ["-P", "-m", "mpremote"]
    assert fl._mpremote("/x/mpremote") == ["/x/mpremote"]


def test_prepare_resets_running_camera_and_returns_serial(monkeypatch):
    raw = flash_config("OPENMV4").raw
    _running(monkeypatch, _Port(0x37C5, 0x1204, "/dev/ttyACM0", "SN9"))
    reset = []
    monkeypatch.setattr(fl.device, "reset", lambda r, cam, **k: reset.append(cam.serial))
    serial = fl._prepare(raw, serial=None, enter_bootloader=True, mpremote=None, dry_run=False)
    assert serial == "SN9" and reset == ["SN9"]


def test_prepare_is_a_noop_in_bootloader_or_dry_run(monkeypatch):
    raw = flash_config("OPENMV4").raw
    monkeypatch.setattr(fl.device, "reset", lambda *a, **k: pytest.fail("should not reset"))
    assert fl._prepare(raw, serial="X", enter_bootloader=False, mpremote=None,
                       dry_run=False) == "X"           # --in-bootloader
    assert fl._prepare(raw, serial="X", enter_bootloader=True, mpremote=None,
                       dry_run=True) == "X"             # dry-run


def test_prepare_none_when_no_running_camera(monkeypatch):
    raw = flash_config("OPENMV4").raw                   # conftest: no cameras
    monkeypatch.setattr(fl.device, "reset", lambda *a, **k: pytest.fail("nothing to reset"))
    assert fl._prepare(raw, serial=None, enter_bootloader=True, mpremote=None,
                       dry_run=False) is None


def test_flash_resets_then_does_not_pin_dfu_serial(project, monkeypatch):
    # a running OPENMV4: mpremote reset selects + enters the bootloader, then dfu-util flashes WITHOUT
    # -S. An OpenMV board's DFU serial is byte-reversed from its runtime serial, so pinning -S with the
    # runtime serial would match nothing (dfu-util -w would hang); the reset already put only this board
    # into DFU, so the vid:pid filter targets it (matching the IDE).
    root, ran, _rec, artifact = project
    artifact("OPENMV4-firmware.bin")
    _running(monkeypatch, _Port(0x37C5, 0x1204, "/dev/ttyACM0", "SN9"))
    fl.flash_firmware(str(root), board="OPENMV4")
    assert ran[0] == [sys.executable, "-P", "-m", "mpremote", "connect", "/dev/ttyACM0",
                      "exec", "import machine; machine.bootloader()"]
    assert "-S" not in ran[1] and "SN9" not in ran[1]   # the dfu flash is NOT serial-pinned


def test_imx_sbl_entry_execs_machine_bootloader(monkeypatch):
    """The imx catcher must reset the camera with `exec machine.bootloader()`, not mpremote's
    `bootloader` subcommand -- the same distinction device.reset() documents for the DFU boards
    (the subcommand silently no-ops on some ports). It matters most here: this call is made to
    REPAIR a corrupt /flash, right after a write to that filesystem failed, so the entry method
    that does the least REPL work is the one to use."""
    import subprocess as sp

    from openmv_ota.flash import flash as fl
    launched = []

    class FakeProc:
        stdout = None

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(fl, "_await_line", lambda *a, **k: True)
    monkeypatch.setattr(fl.device, "select",
                        lambda raw, serial: fl.device.Camera("/dev/ttyACM0", "SN"))
    monkeypatch.setattr(sp, "Popen", lambda argv, **kw: launched.append(argv) or FakeProc())
    fl._imx_catch_and_reset({"blhost": {"usb": "1fc9,0135"}}, "python3", "mpremote", None)

    reset_cmd = [a for a in launched if "connect" in a][0]
    assert "exec" in reset_cmd and "machine.bootloader()" in " ".join(reset_cmd)
    assert reset_cmd[-2:] != ["bootloader"], "must not use the bootloader subcommand"


def test_flash_firmware_on_a_stub_board_writes_the_composed_image(project, monkeypatch):
    """The N6's FIRMWARE partition starts with the boot stub; the firmware is linked to run
    behind it. One DFU download carries both: stub, 0xFF to the firmware's destination, firmware."""
    root, ran, recorded, artifact = project
    artifact("OPENMV_N6-firmware.bin", b"F" * 32)
    with pytest.raises(FlashError, match="OPENMV_N6-stub.bin -- `build firmware` produces it"):
        fl.flash_firmware(str(root), board="OPENMV_N6")
    artifact("OPENMV_N6-stub.bin", b"S" * 16)
    wrote = _capture(monkeypatch, ran)
    steps = fl.flash_firmware(str(root), board="OPENMV_N6")
    [(combined, data)] = wrote.values()
    assert [s.file for s in steps] == [combined] and [s.alt for s in steps] == [1]
    assert combined.name == "OPENMV_N6-firmware-with-stub.bin" and _private(combined, root)
    assert data[:16] == b"S" * 16 and data[0x10000:] == b"F" * 32 and len(data) == 0x10000 + 32
    assert ran[-1][-2:] == ["-D", str(combined)]
    assert recorded[-1]["files"] == [{"file": "OPENMV_N6-firmware-with-stub.bin", "alt": 1}]
    # the factory program carries it the same way
    artifact("OPENMV_N6-factory-romfs.img")
    steps = fl.flash_factory(str(root), board="OPENMV_N6")
    assert [s.file.name for s in steps] == ["OPENMV_N6-firmware-with-stub.bin",
                                           "OPENMV_N6-factory-romfs.img"]


def test_flash_firmware_on_an_h7_board_writes_the_second_text_region_too(project, monkeypatch):
    """The H7 Plus's firmware has a second text region in the bootloader's FILESYSTEM
    partition (alt 1): written first, so the composed image's step stays the one that resets.
    An empty region (the packer moved nothing) is skipped."""
    root, ran, recorded, artifact = project
    artifact("OPENMV4P-firmware.bin", b"F" * 64)
    artifact("OPENMV4P-stub.bin", b"S" * 16)
    artifact("OPENMV4P-firmware-text2.bin", b"T" * 48)
    wrote = _capture(monkeypatch, ran)
    steps = fl.flash_firmware(str(root), board="OPENMV4P")
    assert [(s.file.name, s.alt) for s in steps] == [("OPENMV4P-firmware-text2.bin", 1),
                                                     ("OPENMV4P-firmware-with-stub.bin", 2)]
    assert wrote["OPENMV4P-firmware-text2.bin"][1] == b"T" * 48
    data = wrote["OPENMV4P-firmware-with-stub.bin"][1]
    assert data[:16] == b"S" * 16 and len(data) == 0x20000 + 64 and data[0x20000:] == b"F" * 64
    assert recorded[-1]["files"] == [{"file": "OPENMV4P-firmware-text2.bin", "alt": 1},
                                     {"file": "OPENMV4P-firmware-with-stub.bin", "alt": 2}]
    artifact("OPENMV4P-firmware-text2.bin", b"")
    steps = fl.flash_firmware(str(root), board="OPENMV4P")
    assert [s.alt for s in steps] == [2]
    artifact("OPENMV4P-factory-romfs.img")
    artifact("OPENMV4P-firmware-text2.bin", b"T" * 48)
    steps = fl.flash_factory(str(root), board="OPENMV4P")
    assert [s.alt for s in steps] == [1, 2, 4]


# --- a firmware binary is flashed only as this machine's `build firmware` wrote it -----------
# `flash factory` wrote whatever build/ held; a project handed over with a binary planted there
# put it on the camera (see openmv_ota.build.provenance).

@pytest.mark.parametrize("board, planted, others", [
    ("OPENMV4", "OPENMV4-firmware.bin", ["OPENMV4-factory-romfs.img"]),
    ("OPENMV_N6", "OPENMV_N6-firmware.bin", ["OPENMV_N6-stub.bin", "OPENMV_N6-factory-romfs.img"]),
    ("OPENMV_N6", "OPENMV_N6-stub.bin", ["OPENMV_N6-firmware.bin", "OPENMV_N6-factory-romfs.img"]),
    ("OPENMV4P", "OPENMV4P-firmware-text2.bin",
     ["OPENMV4P-firmware.bin", "OPENMV4P-stub.bin", "OPENMV4P-factory-romfs.img"]),
    ("OPENMV_AE3", "OPENMV_AE3-firmware-M55_HE.bin",
     ["OPENMV_AE3-stub.bin", "OPENMV_AE3-firmware-M55_HP.bin", "OPENMV_AE3-factory-romfs.img",
      "OPENMV_AE3-coprocessor-romfs.img"]),
])
def test_flash_factory_refuses_a_firmware_binary_this_machine_did_not_build(
        project, board, planted, others):
    root, ran, recorded, artifact = project
    for name in others:
        artifact(name)
    (root / "build" / planted).write_bytes(b"planted")
    with pytest.raises(FlashError, match=r"%s \(sha256 %s\) was not built by `openmv-ota build "
                       r"firmware` on this machine" % (planted, hashlib.sha256(b"planted").hexdigest())):
        fl.flash_factory(str(root), board=board)
    assert ran == [] and recorded == []


def test_flash_firmware_refuses_a_firmware_changed_since_it_was_built(project):
    root, ran, _rec, artifact = project
    artifact("OPENMV4-firmware.bin", b"built here")
    (root / "build" / "OPENMV4-firmware.bin").write_bytes(b"changed")
    with pytest.raises(FlashError, match="changed since `openmv-ota build firmware` wrote it"):
        fl.flash_firmware(str(root), board="OPENMV4")
    assert ran == []


def test_a_firmware_flash_needs_the_projects_lock_and_a_romfs_flash_does_not(tmp_path, monkeypatch):
    monkeypatch.setattr(fl.runner, "run", lambda argv, **kw: None)
    monkeypatch.setattr(fl.tools, "find_dfu_util", lambda override, sdk_home: "DFU")
    monkeypatch.setattr(fl.history, "record", lambda *a, **k: None)
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "OPENMV4-firmware.bin").write_bytes(b"x")
    (tmp_path / "build" / "OPENMV4-romfs.img").write_bytes(b"x")
    with pytest.raises(FlashError, match="no openmv-ota.lock.json found"):
        fl.flash_firmware(str(tmp_path), board="OPENMV4", dry_run=True)
    assert fl.flash_romfs(str(tmp_path), board="OPENMV4", dry_run=True)


def test_imx_refuses_a_firmware_this_machine_did_not_build(imx_project):
    root, _ran, _rec = imx_project
    (root / "build" / "OPENMV_RT1060-firmware.bin").write_bytes(b"planted")
    for verb in (fl.flash_firmware, fl.flash_factory):
        with pytest.raises(FlashError, match=r"OPENMV_RT1060-firmware\.bin .* changed since"):
            verb(str(root), board="OPENMV_RT1060", dry_run=True)


def test_imx_bare_firmware_is_checked_too(imx_project, monkeypatch):
    """A board with no stub flashes the firmware as built: that file is the one checked."""
    root, _ran, _rec = imx_project
    monkeypatch.setattr(fl, "_firmware_image", lambda board, out_dir, check: None)
    assert fl.flash_firmware(str(root), board="OPENMV_RT1060", dry_run=True)
    (root / "build" / "OPENMV_RT1060-firmware.bin").write_bytes(b"planted")
    with pytest.raises(FlashError, match="changed since"):
        fl.flash_firmware(str(root), board="OPENMV_RT1060", dry_run=True)


@pytest.mark.parametrize("planted", ["cyw4343_btfw.bin", "ARDUINO_PORTENTA_H7-firmware-text2.bin",
                                     "ARDUINO_PORTENTA_H7-stub.bin"])
def test_arduino_refuses_a_binary_this_machine_did_not_build(arduino_project, planted):
    root, ran = arduino_project
    (root / "build" / planted).write_bytes(b"planted")
    with pytest.raises(FlashError, match=r"%s .* changed since" % re.escape(planted)):
        fl.flash_factory(str(root), board="ARDUINO_PORTENTA_H7", dry_run=True)
    assert ran == []


def test_arduino_bare_firmware_is_checked_too(arduino_project, monkeypatch):
    root, _ran = arduino_project
    monkeypatch.setattr(fl, "_firmware_image", lambda board, out_dir, check: None)
    assert fl.flash_firmware(str(root), board="ARDUINO_PORTENTA_H7", dry_run=True)
    (root / "build" / "ARDUINO_PORTENTA_H7-firmware.bin").write_bytes(b"planted")
    with pytest.raises(FlashError, match="changed since"):
        fl.flash_firmware(str(root), board="ARDUINO_PORTENTA_H7", dry_run=True)


def test_the_tool_writes_the_bytes_that_were_checked(project, monkeypatch):
    """dfu-util opened the path in build/ again, after the check: whatever was there by then was
    written. It now reads a private copy of the checked bytes, removed when the flash is over."""
    root, ran, _rec, artifact = project
    artifact("OPENMV4-firmware.bin", b"built here")
    wrote = {}

    def run(argv, **kw):
        (root / "build" / "OPENMV4-firmware.bin").write_bytes(b"swapped in after the check")
        f = Path(argv[-1])
        wrote["data"], wrote["mode"] = f.read_bytes(), f.stat().st_mode & 0o777
        wrote["dir"] = f.parent.stat().st_mode & 0o777
    monkeypatch.setattr(fl.runner, "run", run)
    steps = fl.flash_firmware(str(root), board="OPENMV4")
    assert wrote == {"data": b"built here", "mode": 0o600, "dir": 0o700}
    assert _private(steps[0].file, root)


def test_the_private_copies_are_removed_when_a_write_fails(project, monkeypatch):
    root, _ran, _rec, artifact = project
    artifact("OPENMV4-firmware.bin")
    seen = []

    def run(argv, **kw):
        seen.append(argv[-1])
        raise FlashError("dfu-util failed")
    monkeypatch.setattr(fl.runner, "run", run)
    with pytest.raises(FlashError, match="dfu-util failed"):
        fl.flash_firmware(str(root), board="OPENMV4")
    assert _private(seen[0], root)


def test_a_dry_run_names_the_files_in_build(project):
    root, _ran, _rec, artifact = project
    artifact("OPENMV4-firmware.bin")
    [step] = fl.flash_firmware(str(root), board="OPENMV4", dry_run=True)
    assert step.file == root / "build" / "OPENMV4-firmware.bin"


def test_flash_refuses_a_firmware_built_for_another_project(project, tmp_path):
    """One `-o` directory shared by two projects pinned to one commit: the firmware there is the
    other project's, with its payload keys and frozen modules."""
    root, ran, _rec, artifact = project
    artifact("OPENMV4-firmware.bin")
    other = tmp_path / "other-project"
    other.mkdir()
    built.record(other, root / "build" / "OPENMV4-firmware.bin")
    with pytest.raises(FlashError, match=r"OPENMV4-firmware\.bin was built for the project at %s"
                       % re.escape(str(other.resolve()))):
        fl.flash_firmware(str(root), board="OPENMV4")
    assert ran == []


def test_flash_refuses_firmware_binaries_from_two_builds(project):
    """New firmware beside a second core an earlier build left: a stub built alone is fine."""
    root, ran, _rec, artifact = project
    for name in ("OPENMV_AE3-stub.bin", "OPENMV_AE3-firmware-M55_HP.bin",
                 "OPENMV_AE3-factory-romfs.img", "OPENMV_AE3-coprocessor-romfs.img"):
        artifact(name)
    built.record(root, root / "build" / "OPENMV_AE3-stub.bin", inputs=None)   # `build stub`
    (root / "build" / "OPENMV_AE3-firmware-M55_HE.bin").write_bytes(b"he")
    built.record(root, root / "build" / "OPENMV_AE3-firmware-M55_HE.bin", inputs="an earlier build")
    with pytest.raises(FlashError, match="were written by different `openmv-ota build firmware` runs"):
        fl.flash_factory(str(root), board="OPENMV_AE3")
    assert ran == []
    built.record(root, root / "build" / "OPENMV_AE3-firmware-M55_HE.bin")
    assert fl.flash_factory(str(root), board="OPENMV_AE3", dry_run=True)

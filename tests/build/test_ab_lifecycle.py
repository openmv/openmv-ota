"""The A/B lifecycle, end to end on a simulated flash.

Every piece of v2 is unit-tested in isolation, but the parts that can strand a device in the
field are the ones that only interact across a whole cycle: the install counter deciding which
slot boots, the anti-rollback floor being carried into each newly-written slot, the attempt
region bounding a trial, and `confirm()` settling it. A bug in any of those looks fine in a
unit test and shows up as a device that boots the wrong image, or downgrades, or never updates
again.

So this drives **the real code** -- `boot.py`'s `OtaBoot`, the installer's `_install_stream`
and `_install_target`, and the runtime lib's confirm/status logic -- against one bytearray
standing in for the partition, across the sequence a real device actually lives:

    provision -> boot A -> install into B -> trial B -> confirm -> install into A -> ...
    ...and the failure arms: a trial that never confirms, and an image that will not verify.

It is deliberately NOT a substitute for the hardware run. Flash here always works, writes
never tear, and nothing reboots mid-erase. What it proves is that the four state machines
agree with each other, which is exactly what no single unit test can.
"""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest

from openmv_ota.build.device import openmv_ota as rt
from openmv_ota.ota import ES256, Trailer, algorithm_for, pack_trailer, signed_region, status
from openmv_ota.ota.keys import generate_private_key, public_point_hex
from openmv_ota.ota.sign import sign_region

_ROOT = Path(__file__).resolve().parents[2]

BLOCK = 4096
SLOT = 8 * BLOCK                  # body + the four control sectors, with room to spare
PARTITION = 2 * SLOT
PRODUCT_ID = 0x1234


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, str(_ROOT / relpath))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


B = _load("openmv_ota._boot_ab", "src/openmv_ota/build/device/boot.py")
INST = _load("openmv_ota._installer_ab", "src/openmv_ota/build/device/openmv_ota/data/installer.py")


# --- a partition that behaves like flash ------------------------------------

class Flash:
    """One bytearray with flash's rules: erase sets 0xFF, a write may only clear bits.

    The 1->0 restriction is the point. Every v2 control write (status markers, the install
    counter, a rollback entry, an attempt byte) is supposed to be a program into erased space,
    and a design that quietly depends on rewriting a byte would pass a test backed by a plain
    bytearray and fail on the device."""

    def __init__(self, size):
        self.mem = bytearray(b"\xff" * size)

    def erase(self, off, size):
        self.mem[off:off + size] = b"\xff" * size

    def write(self, off, data):
        for i, byte in enumerate(data):
            old = self.mem[off + i]
            if byte & ~old:
                raise AssertionError(
                    "write at %d would SET bits (0x%02x -> 0x%02x): flash cannot do that "
                    "without an erase" % (off + i, old, byte))
            self.mem[off + i] = byte

    def read(self, off, size):
        return self.mem[off:off + size]


# --- building signed images -------------------------------------------------

_SPEC = algorithm_for(ES256)
_KEY = generate_private_key(_SPEC)
_KEY_ID = 0x0100
TRUSTED = {_KEY_ID: bytes.fromhex(public_point_hex(_KEY.public_key()))}


def _verify(alg, pubkey_bytes, sig, msg):
    """The injected verify seam, exactly as boot.py calls it: raw point bytes in (that is what
    the firmware stamps into TRUSTED_KEYS), a bool out."""
    from openmv_ota.ota import keys as key_mod
    from openmv_ota.ota import sign
    spec = algorithm_for(alg)
    return sign.verify_region(key_mod.public_key_from_hex(pubkey_bytes.hex(), spec),
                              msg, sig, spec)


def _image(version, *, body_tag=b"APP", product_id=PRODUCT_ID, corrupt=False):
    """A full slot image: body + 0xFF pad + blank control sectors + a signed trailer.

    Exactly the shape `build ota-romfs` renders and the installer streams in -- control
    sectors blank, because the counter and the floor are DEVICE state the installer stamps."""
    body = body_tag + b"." + version.encode() + b"\x00" * 64
    cap = SLOT - 2 * BLOCK
    pad = cap - len(body)
    t = Trailer(body_size=len(body), pad_size=pad, meta={"v": version}, product_id=product_id,
                min_platform_version=0, payload_version=_pv(version), reserved0=0,
                key_id=_KEY_ID, sig_alg=ES256, body_sha256=hashlib.sha256(body).digest())
    t.signature = sign_region(_KEY, signed_region(t), _SPEC)
    trailer = pack_trailer(t)
    img = bytearray(b"\xff" * SLOT)
    img[0:len(body)] = body
    img[SLOT - BLOCK:SLOT - BLOCK + len(trailer)] = trailer
    if corrupt:
        img[0] ^= 0xFF                          # break the body sha -> the slot must be rejected
    return bytes(img)


def _pv(version):
    major, minor, patch = (int(x) for x in version.split("."))
    return (major << 24) | (minor << 16) | (patch << 8)


class _Source:
    """A readinto() source over fixed bytes, dribbling to exercise the re-chunking fill."""

    def __init__(self, data, step=1500):
        self.data, self.pos, self.step = data, 0, step

    def readinto(self, mv):
        n = min(len(mv), self.step, len(self.data) - self.pos)
        mv[:n] = self.data[self.pos:self.pos + n]
        self.pos += n
        return n


# --- the device under simulation --------------------------------------------

class Device:
    """A board: one flash, plus the boot/install/confirm operations a real one performs."""

    def __init__(self, max_attempts=3, single=False):
        self.flash = Flash(PARTITION if not single else SLOT)
        self.single = single
        self.max_attempts = max_attempts
        self.slot = None                 # what the last boot mounted
        self.version = None
        self.reject_reason = None

    # ...the config boot.py and the runtime lib read
    @property
    def cfg(self):
        dev = self

        class _Cfg:
            PARTITION_SIZE = len(dev.flash.mem)
            FRONT_SIZE = 0 if dev.single else SLOT
            CONTROL_BLOCK = BLOCK
            PRODUCT_ID = 0
            TRUSTED_KEYS = TRUSTED
            PLATFORM_VERSION = 0
            MAX_ATTEMPTS = dev.max_attempts
            last_slot = dev.slot
        return _Cfg

    def provision(self, version):
        """`build factory-romfs`: the same image in both slots, ordered by install counter."""
        img = _image(version)
        for i, (off, counter) in enumerate(self._provision_slots()):
            self.flash.write(off, img)
            sector = off + SLOT - 2 * BLOCK
            self.flash.write(sector, status.build_status_sector(
                BLOCK, pending=False, tried=False, confirmed=True, counter=counter,
                floor_version=_pv(version)))
            del i

    def _provision_slots(self):
        if self.single:
            return [(0, 1)]
        return [(0, 2), (SLOT, 1)]

    def boot(self):
        """Run the real `OtaBoot` over this flash. Returns the mounted slot name."""
        mounted = {}

        def mount(body):
            mounted["body"] = bytes(body)

        ob = B.OtaBoot(self.flash.read, _verify, mount, self.flash.write,
                       len(self.flash.mem), 0 if self.single else SLOT, BLOCK,
                       0, TRUSTED, 0, self.max_attempts)
        slot, trailer, reason = ob.run()
        self.slot, self.version, self.reject_reason = slot, trailer.payload_version, reason
        assert mounted["body"].startswith(b"APP.")
        return slot

    def install(self, version, *, corrupt=False):
        """What the installer does: pick the non-running slot, erase it, stream the image in,
        then stamp the representation, the carried floor, the counter and PENDING."""
        slots = INST._slot_table(len(self.flash.mem), 0 if self.single else SLOT)
        counters, floors = {}, {}
        for name, off, size in slots:
            sector = self.flash.read(off + size - 2 * BLOCK, BLOCK)
            counters[name] = INST._install_counter(sector)
            floors[name] = INST._rollback_floor_of(sector[INST._FLOOR_OFF:])
        floor = max(floors.values())
        target, target_off, slot_size, counter = INST._install_target(slots, self.slot, counters)

        self.flash.erase(target_off, slot_size)
        INST._install_stream(
            _Source(_image(version, corrupt=corrupt)),
            lambda off, data: self.flash.write(target_off + off, data),
            lambda off, n: bytes(self.flash.read(target_off + off, n)),
            slot_size, BLOCK, lambda: None, None, None, INST.REPR_FULL, None, None,
            counter, floor)
        return target

    def confirm(self):
        """The runtime lib's confirm(): advance the floor in the RUNNING slot, then CONFIRMED."""
        off, size = rt._slot_bounds(self.cfg, self.slot)
        sector_off = off + size - 2 * BLOCK
        sector = self.flash.read(sector_off, 3 * rt.MARKER_SIZE)
        if not rt._should_confirm(self.slot, sector):
            return False
        roll_off = off + size - 2 * BLOCK + rt._FLOOR_OFF
        roll = self.flash.read(off + size - 2 * BLOCK, BLOCK)[rt._FLOOR_OFF:]
        if rt._rollback_floor_of(roll) < self.version:
            pos = rt._rollback_append_offset(roll)
            self.flash.write(roll_off + pos, rt._rollback_entry(self.version))
        self.flash.write(sector_off + rt._CONFIRMED_OFF, rt.CONFIRMED)
        return True

    def slots_report(self):
        """The runtime lib's slots() -- built from the same reads it does on-device."""
        out = []
        for name in rt._slot_names(self.cfg):
            off, size = rt._slot_bounds(self.cfg, name)
            sector = self.flash.read(off + size - 2 * BLOCK, rt._SLOT_READ)
            trailer = self.flash.read(off + size - BLOCK, rt._TRAILER_READ)
            out.append(rt._slot_report(name, self.slot, sector, rt._trailer_version(trailer)))
        out.sort(key=rt._counter_key, reverse=True)
        return out


# --- the happy path, twice, because alternation is the whole point ----------

def test_updates_alternate_slots_and_never_overwrite_the_running_image():
    dev = Device()
    dev.provision("1.0.0")
    assert dev.boot() == "A"                       # higher install counter wins

    assert dev.install("1.1.0") == "B"             # never the running slot
    assert dev.boot() == "B" and dev.version == _pv("1.1.0")
    assert dev.reject_reason is None               # nothing was rejected getting here
    assert dev.confirm() is True

    # ...and the NEXT update goes back to A. This is the alternation that a v1-shaped
    # installer (always FRONT) would get wrong, and it is why the end-state assertion in the
    # HIL harness cannot pin a slot name.
    assert dev.install("1.2.0") == "A"
    assert dev.boot() == "A" and dev.version == _pv("1.2.0")
    assert dev.confirm() is True

    # the fallback is now 1.1.0 -- the last release that WORKED, not the provisioned image
    report = dev.slots_report()
    assert [s["slot"] for s in report] == ["A", "B"]          # newest first
    assert report[0]["running"] and report[0]["payload_version"] == _pv("1.2.0")
    assert report[1]["payload_version"] == _pv("1.1.0")


def test_confirm_is_idempotent_and_only_touches_the_running_slot():
    dev = Device()
    dev.provision("1.0.0")
    dev.boot()
    dev.install("1.1.0")
    dev.boot()
    assert dev.confirm() is True
    assert dev.confirm() is False                  # already settled -- no second write
    # the OTHER slot's status is untouched by any of it
    other = dev.flash.read(SLOT - 2 * BLOCK, BLOCK) if dev.slot == "B" else \
        dev.flash.read(2 * SLOT - 2 * BLOCK, BLOCK)
    assert other[rt._CONFIRMED_OFF:rt._CONFIRMED_OFF + 16] == rt.CONFIRMED


# --- the failure arms -------------------------------------------------------

def test_a_trial_that_never_confirms_is_rejected_after_its_attempts():
    """The anti-brick path. Each boot burns one attempt BEFORE mounting, so this converges
    even if the trial image hangs -- which is the case a boot-counted-after-running misses."""
    dev = Device(max_attempts=3)
    dev.provision("1.0.0")
    dev.boot()
    dev.install("1.1.0")

    for _ in range(3):                              # three boots, never confirmed
        assert dev.boot() == "B"
    assert dev.boot() == "A"                        # attempts spent -> back to the old release
    assert dev.version == _pv("1.0.0")
    assert "B:trial-failed" in dev.reject_reason


def test_max_attempts_of_one_reproduces_v1():
    dev = Device(max_attempts=1)
    dev.provision("1.0.0")
    dev.boot()
    dev.install("1.1.0")
    assert dev.boot() == "B"                        # its single try
    assert dev.boot() == "A"                        # ...and it is done


def test_an_image_that_does_not_verify_is_never_booted():
    dev = Device()
    dev.provision("1.0.0")
    dev.boot()
    dev.install("1.1.0", corrupt=True)              # installs fine, but the body sha is wrong
    assert dev.boot() == "A"                        # so it is simply not the newest VALID slot
    assert dev.version == _pv("1.0.0")
    assert "B:body-sha" in dev.reject_reason


def test_a_failed_update_can_be_retried_into_the_same_slot():
    """After a rejection the device is running A again, so the retry targets B -- the slot
    holding the bad image. A rule that picked "the lower counter" without excluding the
    running slot would send the retry to A and overwrite the only working image."""
    dev = Device()
    dev.provision("1.0.0")
    dev.boot()
    dev.install("1.1.0", corrupt=True)
    dev.boot()
    assert dev.install("1.1.0") == "B"              # the bad slot, not the running one
    assert dev.boot() == "B" and dev.confirm() is True


# --- the invariants that only a full cycle can break ------------------------

def test_the_fallback_survives_every_confirm():
    """The property A/B exists for, across repeated updates: after each confirm the device must
    STILL have a bootable previous release.

    This was broken and hardware found it. `confirm()` raises the floor to the running version,
    so the slot behind it is below the floor by construction -- and the boot-time anti-rollback
    check was rejecting it, leaving the device one bad update from nothing to return to. A Nicla
    logged `boot: rejected A:rollback` right after promoting 1.1.0."""
    dev = Device()
    dev.provision("1.0.0")
    dev.boot()
    for version in ("1.1.0", "1.2.0", "1.3.0"):
        dev.install(version)
        dev.boot()
        assert dev.confirm() is True
        # the OTHER slot -- the previous release -- must still evaluate as bootable
        other = "B" if dev.slot == "A" else "A"
        off, size = rt._slot_bounds(dev.cfg, other)
        body = dev.flash.read(off, size - 2 * BLOCK)
        status = dev.flash.read(off + size - 2 * BLOCK, BLOCK)
        trailer = dev.flash.read(off + size - BLOCK, BLOCK)
        floor = max(INST._rollback_floor_of(
                        dev.flash.read(o + SLOT - 2 * BLOCK, BLOCK)[INST._FLOOR_OFF:])
                    for o in (0, SLOT))
        t, _consume = B.evaluate_slot(body, status, trailer, floor, 0, TRUSTED, 0, _verify, 3)
        assert t.payload_version < floor      # it IS below the floor...
        # ...and is still bootable, which is the whole point


def test_the_install_carries_the_floor_into_the_slot_it_writes():
    """The floor lives in the slots, and an install ERASES one. It survives only because the
    installer copies the current floor into the slot it writes.

    Single-image is where this is load-bearing and where it is provable: there is one slot, so
    the erase takes the only copy of the floor with it. Verified by mutation -- stub out the
    carry-forward and this floor reads 0, meaning the device would re-admit ANY older signed
    release. (Under A/B the confirmed slot usually still holds a recent entry, so the same bug
    hides; that is exactly why the check belongs here rather than only in the A/B path.)"""
    dev = Device(single=True)
    dev.provision("1.0.0")
    dev.boot()
    dev.install("1.1.0")
    floor = INST._rollback_floor_of(dev.flash.read(SLOT - 2 * BLOCK, BLOCK)[INST._FLOOR_OFF:])
    assert floor == _pv("1.0.0"), "the erase destroyed the floor -- it was not carried forward"
    # ...and the floor it carried still refuses an older signed release
    assert INST._update_reject({"schema": 1, "payload_version": _pv("0.9.0")},
                               0, 0, floor) == "rollback"


def test_the_rollback_floor_never_regresses_across_alternating_installs():
    """The A/B half of the same property: across repeated install -> boot -> confirm cycles,
    the max floor across slots only ever rises."""
    dev = Device()
    dev.provision("1.0.0")
    dev.boot()
    seen = []
    for version in ("1.1.0", "1.2.0", "1.3.0"):
        dev.install(version)
        dev.boot()
        dev.confirm()
        floors = [INST._rollback_floor_of(dev.flash.read(off + SLOT - 2 * BLOCK, BLOCK)[INST._FLOOR_OFF:])
                  for off in (0, SLOT)]
        seen.append(max(floors))
    assert seen == sorted(seen) and seen[-1] == _pv("1.3.0")

    # and the floor is now high enough that an older signed release is refused outright
    assert INST._update_reject({"schema": 1, "payload_version": _pv("1.1.0")},
                               0, 0, seen[-1]) == "rollback"


def test_the_install_counter_only_ever_rises():
    dev = Device()
    dev.provision("1.0.0")
    dev.boot()
    counters = []
    for version in ("1.1.0", "1.2.0", "1.3.0", "1.4.0"):
        dev.install(version)
        dev.boot()
        dev.confirm()
        counters.append(max(
            INST._install_counter(dev.flash.read(off + SLOT - 2 * BLOCK, BLOCK)) or 0
            for off in (0, SLOT)))
    assert counters == sorted(set(counters)) == counters


def test_the_same_version_can_be_installed_twice():
    """Reinstall has to work -- it is why ordering is a counter and not the version. Two slots
    holding 1.1.0 must still have a defined, correct boot order."""
    dev = Device()
    dev.provision("1.0.0")
    dev.boot()
    dev.install("1.1.0")
    dev.boot()
    dev.confirm()
    assert dev.install("1.1.0") == "A"              # the same version, into the other slot
    assert dev.boot() == "A"                        # the NEWER install wins on counter alone
    assert dev.version == _pv("1.1.0")


def test_updates_are_deferred_until_the_running_trial_settles():
    """The device refuses an update while unproven, because the slot it would overwrite is the
    last release known to work. The refusal ends the moment the app confirms."""
    dev = Device()
    dev.provision("1.0.0")
    dev.boot()
    dev.install("1.1.0")
    dev.boot()                                      # running the trial, not confirmed

    st = {"trial": True}
    assert rt._defer_install(st, dev.slots_report()) == "running an unconfirmed trial"
    dev.confirm()
    assert rt._defer_install({"trial": False}, dev.slots_report()) is None


# --- single-image mode ------------------------------------------------------

def test_single_image_mode_updates_its_only_slot():
    """No fallback by design: the target IS the running image. It still trials, confirms,
    counts and carries its floor -- the difference is only that a failure has nowhere to go,
    which is why that mode's failure path is recovery."""
    dev = Device(single=True)
    dev.provision("1.0.0")
    assert dev.boot() == "A"
    assert dev.install("1.1.0") == "A"
    assert dev.boot() == "A" and dev.version == _pv("1.1.0")
    assert dev.confirm() is True
    assert len(dev.slots_report()) == 1
    # and the gate does not hold a one-slot device hostage to its own trial
    assert rt._defer_install({"trial": True}, dev.slots_report()) is None


def test_single_image_mode_has_nowhere_to_fall_back_to():
    dev = Device(single=True)
    dev.provision("1.0.0")
    dev.boot()
    dev.install("1.1.0", corrupt=True)
    with pytest.raises(B.OtaReject, match="no-slot"):
        dev.boot()

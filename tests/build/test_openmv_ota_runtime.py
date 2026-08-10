"""Host tests for the device-side ``openmv_ota`` runtime library (the package
scaffolded into a project's ``app/lib/openmv_ota/``).

Like ``test_device_boot`` for ``boot.py``, this exercises the pure logic on the
host and pins the duplicated status-marker constants against the originals in
``openmv_ota.ota.status`` so they can't drift. The device entry points
(``status``/``confirm``/``sync``) wire MicroPython-only I/O and are covered under
QEMU, not here.
"""

from __future__ import annotations

import pytest

from openmv_ota.build.device import openmv_ota as rt
from openmv_ota.ota import status as host_status


def _sector(pending, tried, confirmed):
    return host_status.build_status_sector(4096, pending=pending, tried=tried,
                                           confirmed=confirmed)


def test_markers_and_offsets_pinned_to_host():
    # The library duplicates these from openmv_ota.ota.status; keep them identical.
    assert (rt.PENDING, rt.TRIED, rt.CONFIRMED) == (
        host_status.PENDING, host_status.TRIED, host_status.CONFIRMED)
    assert (rt.REPR_FULL, rt.REPR_DELTA) == (host_status.REPR_FULL, host_status.REPR_DELTA)
    assert (rt._PENDING_OFF, rt._TRIED_OFF, rt._CONFIRMED_OFF, rt._REPR_OFF) == (
        host_status.PENDING_OFFSET, host_status.TRIED_OFFSET, host_status.CONFIRMED_OFFSET,
        host_status.REPR_OFFSET)
    assert rt.MARKER_SIZE == host_status.MARKER_SIZE


def test_rollback_mirror_matches_host():
    from openmv_ota.ota import rollback as host
    assert rt._ROLLBACK_ENTRY == host.ENTRY_SIZE
    assert rt._rollback_entry(0x01020000) == host.encode_entry(0x01020000)
    sector = bytearray(b"\xff" * 4096)
    sector[0:host.ENTRY_SIZE] = host.encode_entry(0x01000000)
    sector[host.ENTRY_SIZE:2 * host.ENTRY_SIZE] = host.encode_entry(0x01030000)
    assert rt._rollback_floor_of(sector) == host.floor_of(sector) == 0x01030000
    assert rt._rollback_append_offset(sector) == host.append_offset(sector) == 2 * host.ENTRY_SIZE
    assert rt._rollback_append_offset(b"\x00" * 4096) is None   # full -> no room


def test_representation_of_decodes_each():
    def sector(repr_marker):
        s = bytearray(_sector(True, True, False))
        if repr_marker is not None:
            s[rt._REPR_OFF:rt._REPR_OFF + rt.MARKER_SIZE] = repr_marker
        return bytes(s)
    assert rt._representation_of(sector(rt.REPR_FULL)) == "full"
    assert rt._representation_of(sector(rt.REPR_DELTA)) == "delta"
    assert rt._representation_of(sector(None)) is None        # unwritten (0xFF) -> None


def test_status_of_confirmed_image():
    s = rt._status_of(_sector(True, True, True))   # post-OTA confirmed / factory shape
    assert s == {"pending": True, "tried": True, "confirmed": True, "trial": False}
    assert rt._needs_confirm(_sector(True, True, True)) is False


def test_slot_bounds_mirror_boot():
    """The SDK reads the running slot's status and rollback sectors; boot.py decides where
    those are. A third copy of the split (boot.py, installer, here) is the price of device
    modules that cannot import the host package -- so pin it."""
    import tests.build.test_device_boot as boot_test        # noqa: PLC0415
    B = boot_test.B

    class _Cfg:
        CONTROL_BLOCK = 4096

    for partition, front in ((8192, 0), (8192, 8192), (8192, 4096), (12288, 4096)):
        _Cfg.PARTITION_SIZE, _Cfg.FRONT_SIZE = partition, front
        ob = B.OtaBoot(None, None, None, None, partition, front, 4096, 0, {}, 0)
        for name, off, size in ob._slots():
            assert rt._slot_bounds(_Cfg, name) == (off, size)
            assert rt._status_offset(_Cfg, name) == off + size - 2 * 4096
        # an unknown/None slot answers for A rather than raising -- a device that never ran
        # boot.py would be looking there anyway
        assert rt._slot_bounds(_Cfg, None) == rt._slot_bounds(_Cfg, "A")


def test_status_of_unconfirmed_trial():
    s = rt._status_of(_sector(True, True, False))  # booted a one-shot trial, not yet kept
    assert s["trial"] is True and s["confirmed"] is False
    assert rt._needs_confirm(_sector(True, True, False)) is True


# confirm() acts when we booted a slot AND that slot holds an un-confirmed trial. The caller
# passes the RUNNING slot's own status sector, so "don't confirm a slot we fell back from" is
# structural now rather than a name comparison -- there is no sector here for a slot we did not
# boot. What is left to guard is boot.py not having run at all.
@pytest.mark.parametrize(("slot", "pending", "tried", "confirmed", "expect"), [
    ("A",  True,  True,  False, True),    # booted a slot holding an un-confirmed trial
    ("B",  True,  False, False, True),    # ...and TRIED is no longer required (v2 counts attempts)
    (None, True,  True,  False, False),   # boot.py never ran -> nothing to confirm
    ("A",  True,  True,  True,  False),   # already confirmed
    ("A",  False, False, False, False),   # nothing set (a blank slot)
])
def test_should_confirm(slot, pending, tried, confirmed, expect):
    assert rt._should_confirm(slot, _sector(pending, tried, confirmed)) is expect


def test_log_reexport_is_a_null_logger_on_host():
    # openmv_log is absent off-device, so openmv_ota.log is a null logger -- the app can
    # call .info/.warning/etc. unconditionally (on-device it's logging.getLogger).
    assert rt.log.debug("d") is None
    assert rt.log.info("i") is None
    assert rt.log.warning("w") is None
    assert rt.log.error("e") is None
    assert rt.log.critical("c") is None


def test_status_of_pending_without_tried_is_still_a_trial():
    """v2: boot.py stopped writing TRIED -- a trial gets several attempts and each is recorded
    by consuming a byte of the attempt region. Requiring TRIED here would mean no trial was
    ever confirmable, so every update would roll back."""
    s = rt._status_of(_sector(True, False, False))
    assert s["trial"] is True and s["tried"] is False
    assert rt._needs_confirm(_sector(True, False, False)) is True


def test_status_of_erased_sector():
    s = rt._status_of(_sector(False, False, False))
    assert s == {"pending": False, "tried": False, "confirmed": False, "trial": False}


def test_markers_decodes_each_flag():
    assert rt._markers(_sector(True, False, True)) == (True, False, True)


def _target(buf):
    """A read_target(off, n) over an in-memory buffer (stands in for the partition)."""
    return lambda off, n: buf[off:off + n]


def test_streams_equal_matching():
    # multi-chunk file that matches the target byte-for-byte
    assert rt._streams_equal([b"abcd", b"ef"], _target(b"abcdef")) is True


def test_streams_equal_mismatch():
    assert rt._streams_equal([b"abcd", b"ef"], _target(b"abXdef")) is False


def test_streams_equal_offset_tracking():
    # a later chunk differing is still caught (offset advances per chunk)
    assert rt._streams_equal([b"ab", b"cd", b"ef"], _target(b"abcdXf")) is False


def test_streams_equal_feeds_per_chunk():
    # the watchdog is fed per compared chunk (the already-applied case reads it all)
    calls = []
    assert rt._streams_equal([b"ab", b"cd"], _target(b"abcd"), lambda: calls.append(1)) is True
    assert len(calls) == 2


class _RecordLog:
    def __init__(self):
        self.lines = []

    def info(self, msg, *a):
        self.lines.append(msg)


def test_progress_logs_only_on_each_ten_percent_step(monkeypatch):
    rec = _RecordLog()
    monkeypatch.setattr(rt, "log", rec)
    p = rt._Progress("coprocessor")
    # within the same 10% bucket -> one line; crossing into the next -> another
    for done in (3, 5, 9, 12, 19, 25):
        p(done, 100)
    assert rec.lines == [
        "coprocessor: 3% (3/100 bytes)",
        "coprocessor: 12% (12/100 bytes)",
        "coprocessor: 25% (25/100 bytes)",
    ]


def test_progress_zero_total_is_full(monkeypatch):
    rec = _RecordLog()
    monkeypatch.setattr(rt, "log", rec)
    rt._Progress("coprocessor")(0, 0)          # empty target -> 100%, never divides by zero
    assert rec.lines == ["coprocessor: 100% (0/0 bytes)"]


def test_check_readback_ok():
    rt._check_readback(b"\xff\xff", b"\xff\xff")          # match -> no raise
    rt._check_readback(bytearray(b"abc"), b"abc")         # bytearray vs bytes, by value


def test_check_readback_mismatch_raises():
    import pytest
    with pytest.raises(OSError):
        rt._check_readback(b"\xff\x00", b"\xff\xff")      # erase/write didn't take


# --- the check-in extension seam (registry + pure body/offer helpers) -----------------------

@pytest.fixture(autouse=True)
def _reset_checkin_registry():
    rt._checkin_contributors.clear()
    rt._checkin_observers.clear()
    yield
    rt._checkin_contributors.clear()
    rt._checkin_observers.clear()


def test_checkin_body_maps_identity_and_status():
    info = {"device_id": "d1", "product_id": 7, "account_id": "acct",
            "board": "OPENMV_N6", "product": "robot", "app_version": "1.2.0"}
    st = {"payload_version": 5, "slot": "A", "representation": "full",
          "fallback_reason": None, "confirmed": True}
    reported = [{"slot": "A", "running": True, "payload_version": 5, "counter": 4,
                 "confirmed": True, "pending": True},
                {"slot": "B", "running": False, "payload_version": 4, "counter": 3,
                 "confirmed": True, "pending": True}]
    body = rt._checkin_body(info, st, reported)
    assert body == {
        "device_id": "d1", "product_id": 7, "account_id": "acct",
        "board": "OPENMV_N6", "product": "robot", "app_version": "1.2.0",
        "payload_version": 5, "slot": "A", "representation": "full",
        "fallback_reason": None, "confirmed": True, "slots": reported,
    }


def test_checkin_body_defaults_for_missing_fields():
    body = rt._checkin_body({}, {})
    assert body["device_id"] == "" and body["product_id"] == 0
    assert body["account_id"] == "" and body["confirmed"] is False
    assert body["payload_version"] == 0
    assert body["slots"] == []          # an older/simpler caller still produces a valid body


def test_slot_names_mirror_boot():
    """slots() must enumerate exactly the slots boot.py believes in -- reporting a B that
    boot.py never reads would put a phantom fallback on an operator's dashboard."""
    import tests.build.test_device_boot as boot_test        # noqa: PLC0415

    class _Cfg:
        CONTROL_BLOCK = 4096

    for partition, front in ((8192, 0), (8192, 8192), (8192, 4096), (12288, 4096)):
        _Cfg.PARTITION_SIZE, _Cfg.FRONT_SIZE = partition, front
        ob = boot_test.B.OtaBoot(None, None, None, None, partition, front, 4096, 0, {}, 0)
        assert rt._slot_names(_Cfg) == [name for name, _o, _s in ob._slots()]


def test_install_counter_mirrors_boot():
    from openmv_ota.ota import status as host_status

    sector = host_status.build_status_sector(4096, pending=True, tried=False,
                                             confirmed=False, counter=11)
    assert rt._install_counter(sector) == 11
    assert rt._install_counter(b"\xff" * 4096) is None     # blank -> unknown
    assert rt._install_counter(b"\xff" * 8) is None        # too short to hold the field


def test_slot_report_is_the_line_a_server_reads():
    from openmv_ota.ota import status as host_status

    sector = host_status.build_status_sector(4096, pending=True, tried=False,
                                             confirmed=False, counter=9)
    assert rt._slot_report("B", "A", sector, 0x01020000) == {
        "slot": "B", "running": False, "payload_version": 0x01020000, "counter": 9,
        "confirmed": False, "pending": True}
    # a blank slot: no counter, nothing set -- reported as-is rather than guessed at
    blank = b"\xff" * 4096
    assert rt._slot_report("B", "B", blank, 0) == {
        "slot": "B", "running": True, "payload_version": 0, "counter": None,
        "confirmed": False, "pending": False}


def test_counter_key_sorts_an_unreadable_counter_last():
    """slots() reports newest-first, and 'newest' has to mean what boot.select_slot means:
    a slot whose counter we cannot read is never claimed to be the newer one."""
    entries = [{"counter": None}, {"counter": 2}, {"counter": 7}]
    assert [e["counter"] for e in sorted(entries, key=rt._counter_key, reverse=True)] == [
        7, 2, None]


def test_trailer_version_offset_is_pinned_to_the_host_trailer():
    """The SDK reads ONE field out of a trailer by raw offset, because the app has no room
    for a trailer parser. Pin the offset against the real packer -- a silent drift here
    would report a plausible-looking wrong version for the fallback slot."""
    import hashlib

    from openmv_ota.ota import ES256, Trailer, signed_region
    from openmv_ota.ota.version import encode_app_version

    pv = encode_app_version("3.4.5")
    t = Trailer(body_size=8, pad_size=0, meta={}, product_id=7, min_platform_version=0,
                payload_version=pv, payload_version_floor=0, key_id=1, sig_alg=ES256,
                body_sha256=hashlib.sha256(b"x").digest())
    assert rt._trailer_version(signed_region(t)) == pv
    assert rt._trailer_version(b"XXXX" + b"\x00" * 40) == 0     # bad magic -> 0
    assert rt._trailer_version(b"\xff" * 4) == 0                # too short -> 0


@pytest.mark.parametrize(("trial", "n_slots", "expect"), [
    (True,  2, "running an unconfirmed trial"),   # the fallback is a PROVEN release: protect it
    (False, 2, None),                             # settled -> the other slot is expendable
    (True,  1, None),                             # single-image: no fallback to protect...
    (False, 1, None),                             # ...and nothing to wait for either
])
def test_defer_install_protects_a_proven_fallback(trial, n_slots, expect):
    """The installer writes the slot we are NOT running -- during a trial, that is the last
    release known to work. Taking a new update then trades a proven fallback for an unproven
    one, at the moment the device has already said it is unsure of itself."""
    st = {"trial": trial}
    assert rt._defer_install(st, [{}] * n_slots) == expect


def test_contributors_merge_into_the_body_and_bad_ones_are_skipped():
    rt.register_checkin(contribute=lambda: {"streams": ["0", "tele"]})
    def boom():
        raise RuntimeError("nope")
    rt.register_checkin(contribute=boom)             # must not break collection
    rt.register_checkin(contribute=lambda: None)     # falsy -> ignored
    body = rt._collect_body({"device_id": "d1"}, {})
    assert body["streams"] == ["0", "tele"]
    assert body["device_id"] == "d1"


def test_observers_all_fire_and_a_raising_one_is_isolated():
    seen = []
    rt.register_checkin(on_response=lambda r: seen.append(("a", r["x"])))
    rt.register_checkin(on_response=lambda r: (_ for _ in ()).throw(ValueError()))
    rt.register_checkin(on_response=lambda r: seen.append(("b", r["x"])))
    rt._notify({"x": 42})
    assert seen == [("a", 42), ("b", 42)]            # both good ones ran


def test_register_with_a_key_is_idempotent_replaces_not_appends():
    calls = []
    rt.register_checkin(contribute=lambda: {"v": 1},
                        on_response=lambda r: calls.append(1), key="ext")
    rt.register_checkin(contribute=lambda: {"v": 2},   # re-import/reload: same key
                        on_response=lambda r: calls.append(2), key="ext")
    body = rt._collect_body({}, {})
    assert body["v"] == 2                              # replaced, not both
    rt._notify({})
    assert calls == [2]                                # only the latest observer


@pytest.mark.parametrize(("resp", "expect"), [
    ({"update": True, "manifest_url": "https://s/m.bin"}, "https://s/m.bin"),
    ({"update": False, "manifest_url": "https://s/m.bin"}, None),
    ({"update": True}, None),
    ({}, None),
])
def test_offer(resp, expect):
    assert rt._offer(resp) == expect

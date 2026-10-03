"""Host tests for ``openmv_rtc`` -- deciding whether the device clock is real.

The trust rule (a clock reading earlier than the build cannot be real), the
epoch conversion (ports disagree: stm32/rp2 count from 2000, alif/mimxrt from
1970), and the RTC tuple layout. ``sync``/``resolve`` need a network and an RTC
and are exercised on hardware.
"""

from __future__ import annotations

import pytest

from openmv_ota.build.device import openmv_rtc as rtc

BUILD = 1_700_000_000          # a plausible build timestamp (Nov 2023)


@pytest.fixture(autouse=True)
def _restore():
    build, source, bad = rtc.BUILD_TIME, rtc._source, rtc._bad
    yield
    rtc.BUILD_TIME, rtc._source, rtc._bad = build, source, bad


def _at(monkeypatch, unix):
    """Pin what the clock reads."""
    monkeypatch.setattr(rtc, "now", lambda: unix)


def _epoch(monkeypatch, year):
    """Pretend the port's epoch is `year` by faking time.gmtime(0)."""
    real = rtc.time.gmtime
    monkeypatch.setattr(rtc.time, "gmtime",
                        lambda *a: (year, 1, 1, 0, 0, 0, 3, 1) if a == (0,) else real(*a))


# --- the epoch difference between ports -------------------------------------

def test_epoch_offset_is_detected_not_assumed(monkeypatch):
    # an AE3 (alif, 1970) and an N6 (stm32, 2000) disagree by 30 years; reading
    # time.time() without this correction puts half the fleet in 1970 or 2054
    _epoch(monkeypatch, 2000)
    assert rtc._epoch_offset() == 946684800
    _epoch(monkeypatch, 1970)
    assert rtc._epoch_offset() == 0


def test_now_returns_unix_seconds_on_a_2000_epoch_port(monkeypatch):
    _epoch(monkeypatch, 2000)
    monkeypatch.setattr(rtc.time, "time", lambda: 0)      # the port's own zero
    assert rtc.now() == 946684800                          # = 2000-01-01 in Unix


def test_now_returns_unix_seconds_on_a_1970_epoch_port(monkeypatch):
    _epoch(monkeypatch, 1970)
    monkeypatch.setattr(rtc.time, "time", lambda: 12345)
    assert rtc.now() == 12345


# --- the window predicate (pure) --------------------------------------------

def test_in_window_accepts_the_build_and_a_bit_after():
    rtc.BUILD_TIME = BUILD
    assert rtc._in_window(BUILD)
    assert rtc._in_window(BUILD + 60)


def test_in_window_rejects_before_the_build():
    # the firmware cannot have run before it was built: a dead RTC reads the
    # epoch year and fails this with no network needed
    rtc.BUILD_TIME = BUILD
    assert not rtc._in_window(BUILD - 1)
    assert not rtc._in_window(0)


def test_in_window_rejects_an_absurd_future():
    # a corrupt RTC latching all ones must not read as a valid far-future time
    rtc.BUILD_TIME = BUILD
    assert not rtc._in_window(BUILD + rtc._MAX_AHEAD + 1)


# --- trust, with the count-up latch -----------------------------------------

def test_a_clock_valid_from_the_first_reading_is_trusted(monkeypatch):
    # survived deep sleep / a coin cell: valid at boot, trusted with no sync
    rtc.BUILD_TIME, rtc._bad = BUILD, False
    _at(monkeypatch, BUILD + 60)
    assert rtc.trusted()


def test_a_clock_below_the_build_is_not_trusted(monkeypatch):
    rtc.BUILD_TIME, rtc._bad = BUILD, False
    _at(monkeypatch, BUILD - 1)
    assert not rtc.trusted()


def test_a_clock_that_counts_up_into_the_window_is_still_not_trusted(monkeypatch):
    # THE case this latch exists for: a dead RTC starts near the epoch and, left
    # running long enough, counts past the build. The first out-of-window reading
    # latches it bad, so the later in-window reading is refused -- it is really
    # epoch + uptime, not a real time.
    rtc.BUILD_TIME, rtc._bad = BUILD, False
    _at(monkeypatch, BUILD - 1000)                 # boot: below the build
    assert not rtc.trusted()                       # ...latches bad
    _at(monkeypatch, BUILD + 1000)                 # later: counted up into range
    assert not rtc.trusted()                       # ...still refused


def test_one_bad_reading_latches_even_after_recovery(monkeypatch):
    # "one invalid reading and we don't trust it": a transient garbage read from
    # an external RTC poisons trust until the clock is actually re-set
    rtc.BUILD_TIME, rtc._bad = BUILD, False
    _at(monkeypatch, BUILD + 60)
    assert rtc.trusted()                           # fine so far
    _at(monkeypatch, 0)                            # one garbage reading
    assert not rtc.trusted()
    _at(monkeypatch, BUILD + 120)                  # back to a sane reading
    assert not rtc.trusted()                       # ...but trust is gone


def test_setting_the_clock_clears_the_latch(monkeypatch):
    # a real set (what NTP sync does) rescues a latched-bad clock
    rtc.BUILD_TIME, rtc._bad = BUILD, True         # latched from a bad boot
    _at(monkeypatch, BUILD + 60)
    assert not rtc.trusted()                       # refused despite a good reading
    _epoch(monkeypatch, 1970)
    monkeypatch.setitem(__import__("sys").modules, "machine",
                        type("m", (), {"RTC": type("r", (), {"datetime": lambda s, t: None})})())
    rtc.set_time(BUILD + 60)
    assert not rtc._bad
    assert rtc.trusted()


def test_without_a_build_stamp_nothing_is_trusted(monkeypatch):
    # a non-OTA firmware has no floor to compare against, so the clock is
    # reported untrusted rather than assumed good
    rtc.BUILD_TIME = 0
    _at(monkeypatch, BUILD)
    assert not rtc.trusted()


# --- what gets attached to a record -----------------------------------------

def test_timestamp_is_none_when_untrusted(monkeypatch):
    rtc.BUILD_TIME, rtc._bad = BUILD, False
    monkeypatch.setattr(rtc, "now", lambda: BUILD - 1000)
    assert rtc.timestamp() is None                 # absent beats wrong


def test_timestamp_is_the_time_when_trusted(monkeypatch):
    rtc.BUILD_TIME, rtc._bad = BUILD, False
    monkeypatch.setattr(rtc, "now", lambda: BUILD + 5)
    assert rtc.timestamp() == BUILD + 5


# --- setting the clock ------------------------------------------------------

def test_set_time_uses_the_portable_datetime_tuple(monkeypatch):
    # datetime() is the ONLY method on all four ports we ship, and its tuple is
    # (year, month, day, weekday, hour, minute, second, subsec) with weekday 1-7
    _epoch(monkeypatch, 1970)
    seen = {}

    class _RTC:
        def datetime(self, t):
            seen["t"] = t

    monkeypatch.setitem(__import__("sys").modules, "machine",
                        type("m", (), {"RTC": _RTC})())
    rtc.set_time(1_700_000_000)                    # 2023-11-14 22:13:20 UTC, a Tuesday
    year, month, day, weekday, hour, minute, second, subsec = seen["t"]
    assert (year, month, day) == (2023, 11, 14)
    assert (hour, minute, second, subsec) == (22, 13, 20, 0)
    assert weekday == 2                            # gmtime wday 1 (Tue) + 1


def test_set_time_converts_out_of_unix_into_the_port_epoch(monkeypatch):
    # On a 2000-epoch port the RTC must be handed the PORT's seconds, not Unix
    # ones -- otherwise the clock lands 30 years out. Assert on what gmtime is
    # asked to convert: the port's own gmtime supplies the calendar fields, so
    # the host's 1970 gmtime here would mask the bug.
    _epoch(monkeypatch, 2000)
    asked = []
    real = rtc.time.gmtime
    monkeypatch.setattr(rtc.time, "gmtime",
                        lambda *a: ((2000, 1, 1, 0, 0, 0, 3, 1) if a == (0,)
                                    else (asked.append(a[0]), real(*a))[1]))
    monkeypatch.setitem(__import__("sys").modules, "machine",
                        type("m", (), {"RTC": type("r", (), {"datetime": lambda s, t: None})})())
    rtc.set_time(946684800 + 60)                   # one minute past the 2000 epoch
    assert asked == [60]                           # the offset was removed first


# --- source reporting -------------------------------------------------------

def test_source_defaults_to_none():
    rtc._source = "none"
    assert rtc.source() == "none"


# --- the server's Date header: the main time source --------------------------

@pytest.fixture
def _clock(monkeypatch):
    """A controllable ticks_ms, a recording RTC, and a clean network-time state."""
    t = {"ms": 1_000_000, "set": []}
    monkeypatch.setattr(rtc, "_ticks_ms", lambda: t["ms"])
    monkeypatch.setattr(rtc, "set_time", lambda unix: t["set"].append(unix))
    monkeypatch.setattr(rtc, "_net_ms", None)
    monkeypatch.setattr(rtc, "_ntp_ms", None)
    rtc.BUILD_TIME, rtc._bad, rtc._source = BUILD, False, "none"
    return t


@pytest.mark.parametrize("y,m,d", [(1970, 1, 1), (2000, 2, 29), (2023, 11, 14), (2036, 2, 7),
                                   (2100, 3, 1), (1999, 12, 31)])
def test_days_from_civil_matches_the_calendar(y, m, d):
    import calendar
    assert rtc._days_from_civil(y, m, d) * 86400 == calendar.timegm((y, m, d, 0, 0, 0))


def test_parse_http_date_reads_an_imf_fixdate():
    assert rtc.parse_http_date(b" Tue, 14 Nov 2023 22:13:20 GMT\r\n") == 1_700_000_000
    assert rtc.parse_http_date("Tue, 14 Nov 2023 22:13:20 GMT") == 1_700_000_000   # str too


@pytest.mark.parametrize("bad", [
    b"", b"garbage", b"Tue, 14 Nov 2023 22:13:20 PST",          # not GMT
    b"Tue, 14 Foo 2023 22:13:20 GMT",                            # no such month
    b"Tue, xx Nov 2023 22:13:20 GMT", b"Tue, 14 Nov 2023 22:13 GMT",
    b"Tue, 32 Nov 2023 22:13:20 GMT", b"Tue, 14 Nov 2023 24:00:00 GMT",
    b"Tue, 14 Nov 2023 22:60:00 GMT", b"Tue, 14 Nov 2023 22:13:61 GMT",
    b"Tuesday, 14-Nov-23 22:13:20 GMT",                          # obsolete RFC 850 form
])
def test_parse_http_date_rejects_what_is_not_an_imf_fixdate(bad):
    assert rtc.parse_http_date(bad) is None


def test_server_date_corrects_a_drifted_rtc(monkeypatch, _clock):
    # THE bench bug: an RTC that reads past the build (so the floor trusts it) but runs
    # 100 s slow. The check-in's Date re-sets it.
    _at(monkeypatch, 1_700_000_000 - 100)
    assert rtc.server_date(b"Tue, 14 Nov 2023 22:13:20 GMT")
    assert _clock["set"] == [1_700_000_000]
    assert rtc.source() == "server" and rtc.fresh()


def test_server_date_leaves_a_close_rtc_alone(monkeypatch, _clock):
    # Date has 1 s resolution: re-setting on every check-in would only add jitter
    _at(monkeypatch, 1_700_000_000 + rtc._DRIFT_S)
    assert rtc.server_date(b"Tue, 14 Nov 2023 22:13:20 GMT")
    assert _clock["set"] == []
    assert rtc.source() == "server" and rtc.fresh()      # but it still counts as checked


def test_server_date_sets_an_untrusted_clock_even_if_close(monkeypatch, _clock):
    rtc._bad = True                                      # latched from a bad boot
    _at(monkeypatch, 1_700_000_000)
    assert rtc.server_date(b"Tue, 14 Nov 2023 22:13:20 GMT")
    assert _clock["set"] == [1_700_000_000]


def test_server_date_ignores_garbage_and_pre_build_times(monkeypatch, _clock):
    _at(monkeypatch, BUILD + 60)
    assert not rtc.server_date(b"not a date")
    assert not rtc.server_date(b"Thu, 01 Jan 2015 00:00:00 GMT")   # before the build: a bad proxy
    assert _clock["set"] == [] and rtc.source() == "none" and not rtc.fresh()


def test_fresh_expires_and_drops_the_stale_mark(monkeypatch, _clock):
    _at(monkeypatch, 1_700_000_000)
    rtc.server_date(b"Tue, 14 Nov 2023 22:13:20 GMT")
    _clock["ms"] += (rtc._RESYNC_S - 1) * 1000
    assert rtc.fresh()
    _clock["ms"] += 1000
    assert not rtc.fresh()
    assert rtc._net_ms is None          # dropped, so a ticks wrap can never revive it
    _clock["ms"] -= 10 ** 9             # even a "negative" age (wrap) never reads as fresh
    assert not rtc.fresh()


def test_ntp_attempts_are_rate_limited(_clock):
    assert rtc._ntp_due()               # first attempt: go
    assert not rtc._ntp_due()           # right after: no
    _clock["ms"] += (rtc._NTP_RETRY_S - 1) * 1000
    assert not rtc._ntp_due()
    _clock["ms"] += 1000
    assert rtc._ntp_due()               # spacing elapsed: go again


def test_ntp_due_after_a_ticks_wrap(_clock):
    rtc._ntp_due()
    _clock["ms"] -= 10 ** 6             # ticks_diff went negative: treat as due, never stuck
    assert rtc._ntp_due()


def test_host_ticks_fallback_counts_milliseconds():
    a = rtc._ticks_ms()
    assert rtc._ticks_diff(a + 5, a) == 5
    assert rtc._age_s(None) is None
    assert rtc._age_s(rtc._ticks_ms()) == 0

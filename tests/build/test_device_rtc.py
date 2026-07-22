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
    build, source = rtc.BUILD_TIME, rtc._source
    yield
    rtc.BUILD_TIME, rtc._source = build, source


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


# --- the trust rule ---------------------------------------------------------

def test_a_clock_at_or_past_the_build_is_trusted():
    rtc.BUILD_TIME = BUILD
    assert rtc.trusted(at=BUILD)
    assert rtc.trusted(at=BUILD + 60)


def test_a_clock_before_the_build_is_not_trusted():
    # the firmware cannot have run before it was built: a dead RTC reads the
    # epoch year and fails this with no network needed
    rtc.BUILD_TIME = BUILD
    assert not rtc.trusted(at=BUILD - 1)
    assert not rtc.trusted(at=0)


def test_an_absurdly_future_clock_is_not_trusted():
    # a corrupt RTC latching all ones must not read as a valid far-future time
    rtc.BUILD_TIME = BUILD
    assert not rtc.trusted(at=BUILD + rtc._MAX_AHEAD + 1)


def test_without_a_build_stamp_nothing_is_trusted():
    # a non-OTA firmware has no floor to compare against, so the clock is
    # reported untrusted rather than assumed good
    rtc.BUILD_TIME = 0
    assert not rtc.trusted(at=BUILD)


# --- what gets attached to a record -----------------------------------------

def test_timestamp_is_none_when_untrusted(monkeypatch):
    rtc.BUILD_TIME = BUILD
    monkeypatch.setattr(rtc, "now", lambda: BUILD - 1000)
    assert rtc.timestamp() is None                 # absent beats wrong


def test_timestamp_is_the_time_when_trusted(monkeypatch):
    rtc.BUILD_TIME = BUILD
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

"""Firmware-resident recovery: the retry policy and the interface plan.

Recovery is the last thing between a device and a bench visit, so the decisions worth testing
are the ones that decide whether it keeps trying and what it tries -- not the flash and socket
work, which is the same code the normal install path already uses and is exercised on hardware.
"""

from __future__ import annotations

import pytest

from openmv_ota.build.device import openmv_netcfg as nc
from openmv_ota.build.device import openmv_recovery as rec

UID = b"\x01\x02\x03\x04"


class _Stop(Exception):
    """Breaks recovery's deliberately infinite retry loop inside a test."""


def test_log_is_a_null_logger_off_device():
    """openmv_log is frozen into firmware and absent on the host, so every call site can log
    unconditionally. Recovery in particular must never fail on a logging call -- there is
    nothing below it to catch the exception."""
    assert rec.log.debug("d") is None
    assert rec.log.info("i") is None
    assert rec.log.warning("w") is None
    assert rec.log.error("e") is None
    assert rec.log.critical("c") is None


def test_backoff_starts_quick_then_settles_and_caps():
    """Most recoveries are a transient server or a router still booting, so the first retries
    are fast. It caps rather than growing without bound: a device down for a day should still
    notice the fix within minutes of it landing, because by then someone is waiting."""
    waits = [rec.backoff_for(i) for i in range(8)]
    assert waits[0] <= 10                                  # try again almost immediately
    assert waits == sorted(waits)                          # never gets faster
    assert waits[-1] == waits[-2] == max(rec.BACKOFF_S)    # ...and plateaus
    assert rec.backoff_for(-1) == rec.BACKOFF_S[0]         # defensive: never negative-indexes


def test_configured_interface_is_tried_first():
    settings = nc.settings(nc.parse("interface = wifi\nwifi.ssid = Net\n"), UID)
    assert rec.interface_plan(settings, has_wifi=True, has_eth=True)[0] == "wifi"


def test_wired_is_tried_even_when_wifi_is_configured():
    """A device with stale credentials is exactly the device that is stranded. If a cable
    happens to be plugged in, trying it costs one attempt and saves a bench visit."""
    settings = nc.settings(nc.parse("interface = wifi\nwifi.ssid = OldNetwork\n"), UID)
    assert rec.interface_plan(settings, has_wifi=True, has_eth=True) == ["wifi", "eth"]


def test_no_settings_falls_back_to_wired():
    """A board on a desk with a cable and no configuration is the common bench case, and DHCP
    on it needs nothing from the user."""
    assert rec.interface_plan(None, has_wifi=True, has_eth=True) == ["eth"]
    assert rec.interface_plan(None, has_wifi=True, has_eth=False) == []


def test_a_board_without_an_interface_never_plans_for_it():
    settings = nc.settings(nc.parse("interface = wifi\nwifi.ssid = Net\n"), UID)
    assert rec.interface_plan(settings, has_wifi=True, has_eth=False) == ["wifi"]
    # ...and a wifi config on a board with no radio plans nothing rather than looping on it
    assert rec.interface_plan(settings, has_wifi=False, has_eth=False) == []


def test_wired_config_also_tries_wifi_when_credentials_exist():
    settings = nc.settings(nc.parse(
        "interface = eth\nwifi.ssid = Net\nwifi.psk = pw\n"), UID)
    assert rec.interface_plan(settings, has_wifi=True, has_eth=True) == ["eth", "wifi"]


def test_settings_kind_identifies_the_configured_interface():
    settings = nc.settings(nc.parse("interface = wifi\nwifi.ssid = Net\n"), UID)
    assert rec.settings_kind(settings) == "wifi"
    assert rec.settings_kind(None) is None


def test_a_fallback_interface_gets_credentials_but_not_the_other_one_s_address(monkeypatch):
    """Two things must be true of the eth->wifi fallback, and an earlier shape got both wrong
    by passing None: wifi CANNOT associate without credentials, and a static address written
    for the wired network is wrong on the wireless one -- applying it would strand a device
    that had a perfectly good DHCP server waiting."""
    seen = []

    def fake_bring_up(kind, settings, static=False):
        seen.append((kind, settings is not None, static))
        return kind == "wifi"                  # wired fails, wifi comes up

    settings = nc.settings(nc.parse(
        "interface = eth\nipv4 = static\nipv4.address = 10.0.0.5\n"
        "ipv4.netmask = 255.255.255.0\nipv4.gateway = 10.0.0.1\n"
        "wifi.ssid = Net\nwifi.psk = pw\n"), UID)
    monkeypatch.setattr(rec, "_read_settings", lambda: ({}, settings))
    monkeypatch.setattr(rec, "_has", lambda cfg, kind: True)
    monkeypatch.setattr(rec, "_bring_up", fake_bring_up)
    installed = []
    monkeypatch.setattr(rec, "_install", lambda cfg: installed.append(cfg.SERVER_URL))
    monkeypatch.setattr(rec, "backoff_for", lambda n: (_ for _ in ()).throw(_Stop()))

    class Cfg:
        SERVER_URL = "https://x/manifest.bin"
        CA_PEM = b""

    with pytest.raises(_Stop):
        rec.run(Cfg)
    assert seen == [("eth", True, True), ("wifi", True, False)]
    assert installed == ["https://x/manifest.bin"]


def test_recovery_refuses_rather_than_spinning_without_a_server(monkeypatch, capsys):
    """No SERVER_URL stamped is a BUILD mistake, and no amount of retrying fixes it. Spinning
    forever would hide it; returning makes the boot fail loudly instead."""
    class Cfg:
        SERVER_URL = ""

    monkeypatch.setattr(rec, "backoff_for", lambda n: (_ for _ in ()).throw(_Stop()))
    rec.run(Cfg)          # returns; does NOT raise _Stop, so it never reached the retry


def test_obfuscation_prefix_is_pinned_to_netcfg():
    """The frozen device modules are flat on-device, so this one duplicates netcfg's prefix
    rather than importing it. Pin them together -- drift would mean recovery rewrote an
    already-obfuscated PSK every boot, obfuscating it twice."""
    assert rec._OBFUSCATED == nc._OBFUSCATED


def test_psk_is_rewritten_once_and_then_left_alone():
    """Rewriting rather than deleting: the file is exactly what is needed NEXT time, and
    silently removing someone's configuration is surprising. Writing only when there is
    something to change keeps both the flash wear and the crash window near zero."""
    assert rec.should_rewrite_psk({"wifi.psk": "hunter2"}) is True
    assert rec.should_rewrite_psk({"wifi.psk": nc.obfuscate("hunter2", UID)}) is False
    assert rec.should_rewrite_psk({}) is False              # nothing to rewrite

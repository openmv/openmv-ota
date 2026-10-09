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
        CA_PEM = b"-----BEGIN CERTIFICATE-----\nxx\n"  # empty only on a TLS_VERIFY = False build

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


# --- Wi-Fi bring-up: WLAN boards and the WINC1500 shield (the H7 Plus) -------------------

class _Net:
    STA_IF = 0

    def __init__(self, wlan, fail=False):
        self.calls = []
        net = self

        class _Nic:
            def __init__(self, *a):
                net.calls.append(("new",) + a)

            def active(self, on):
                net.calls.append(("active", on))

            def connect(self, ssid, *a, **kw):
                net.calls.append(("connect", ssid) + a + tuple(sorted(kw.items())))
                if fail:
                    raise OSError("could not connect to ssid=%s, key=secret" % ssid)

        setattr(self, "WLAN" if wlan else "WINC", _Nic)


def test_wifi_is_found_on_wlan_and_on_the_winc_shield():
    """Recovery used to probe only network.WLAN, so a WINC board never tried its Wi-Fi."""
    assert rec.has_wifi(_Net(wlan=True)) and rec.has_wifi(_Net(wlan=False))
    assert not rec.has_wifi(object())


def test_join_wifi_builds_a_fresh_wlan():
    net = _Net(wlan=True)
    rec.join_wifi(net, "lab", "pw", nc)
    assert net.calls == [("new", 0), ("active", True), ("connect", "lab", "pw")]


def test_join_wifi_uses_the_winc_with_a_keyword_key():
    net = _Net(wlan=False)
    rec.join_wifi(net, "lab", "pw", nc)
    assert net.calls == [("new",), ("connect", "lab", ("key", "pw"))]
    net = _Net(wlan=False)
    rec.join_wifi(net, "cafe", "", nc)                     # open network: no key at all
    assert net.calls[-1] == ("connect", "cafe", ("key", None))


def test_a_failed_winc_join_is_survived_and_the_key_is_not_logged(monkeypatch):
    """Recovery must never die, and the WINC's error message carries the key."""
    lines = []
    monkeypatch.setattr(rec.log, "warning", lambda m, *a: lines.append(m))
    nic = rec.join_wifi(_Net(wlan=False, fail=True), "lab", "secret", nc)
    assert nic is not None
    assert lines and all("secret" not in m for m in lines)


# --- the check-in -------------------------------------------------------------
# SERVER_URL is the server, not a manifest: recovery asks /api/v1/check what to install. Built
# against the bench, where installer.run(SERVER_URL) followed the server's / -> /docs redirect
# and died on "install URL must be https:// (got '/docs')".

class _Cfg:
    SERVER_URL = "https://ota.example"
    PRODUCT_ID = 42
    ACCOUNT_ID = "acct_x"
    BOARD = "OPENMV4"


def _split(req):
    head, _, body = req.partition(b"\r\n\r\n")
    return head.decode().split("\r\n"), body


def test_checkin_request_is_the_runtime_post_from_the_firmware_constants():
    import json
    lines, body = _split(rec.checkin_request(_Cfg, "ota.example", 443, "ABC123", "stm32", UID))
    assert lines[0] == "POST /api/v1/check HTTP/1.0"     # 1.0: no proxy may chunk the reply
    assert "Host: ota.example" in lines
    assert "Content-Type: application/json" in lines
    assert "Content-Length: %d" % len(body) in lines
    assert json.loads(body) == {
        "device_id": "ABC123", "product_id": 42, "account_id": "acct_x", "board": "OPENMV4",
        "payload_version": 0, "orders_by_seq": False, "fallback_reason": "recovery"}


def test_checkin_request_names_a_non_default_port_and_orders_a_stock_lineage_by_seq():
    import json

    class Cfg:                                             # an older firmware: no BOARD stamp
        PRODUCT_ID = 0

    lines, body = _split(rec.checkin_request(Cfg, "h", 8443, "X", "stm32", UID))
    assert "Host: h:8443" in lines
    sent = json.loads(body)
    assert sent["orders_by_seq"] is True and sent["board"] is None and sent["account_id"] == ""


def test_checkin_request_rebuilds_the_alif_id_and_only_there():
    """The Alif's omv.board_id() is empty; the id is the UID bytes padded to 12, uppercase --
    the same arithmetic as the runtime's _board_id_fallback. Nowhere else is it guessed."""
    import json
    uid = bytes.fromhex("0a0b0c0d0e0f1011")

    def dev(board_id, platform, u=uid):
        return json.loads(_split(rec.checkin_request(_Cfg, "h", 443, board_id, platform, u))[1])[
            "device_id"]

    assert dev("", "alif") == "0A0B0C0D0E0F101100000000"
    assert dev("REAL", "alif") == "REAL"                   # a real id always wins
    assert dev("", "stm32") == ""                          # -> the server's 422, logged
    assert dev("", "alif", bytes(13)) == ""                # not the 8-byte id this port gives
    assert dev(None, "alif", b"") == ""


def test_the_offer_is_installed_and_nothing_or_a_throttle_waits():
    assert rec.offered(200, b'{"update": true, "manifest_url": "https://h/d/t/m.bin"}') \
        == "https://h/d/t/m.bin"
    assert rec.offered(200, b'{"update": false, "poll_after_s": 300}') is None
    assert rec.offered(429, b"anything") is None           # the server pacing a crowd


def test_a_refusal_raises_so_the_loop_logs_it_and_backs_off():
    for code in (422, 404, 500, 302):
        with pytest.raises(OSError, match="HTTP %d" % code):
            rec.offered(code, b"{}")


def test_the_server_answering_ends_the_attempt(monkeypatch):
    """Nothing offered is an answer, not a failed interface: recovery waits its backoff and asks
    again, rather than tearing down a working link to try the next one."""
    seen = []
    monkeypatch.setattr(rec, "_read_settings", lambda: ({}, None))
    monkeypatch.setattr(rec, "_has", lambda cfg, kind: True)
    monkeypatch.setattr(rec, "_bring_up", lambda kind, s, static=False: seen.append(kind) or True)
    monkeypatch.setattr(rec, "_install", lambda cfg: None)       # the server had nothing
    monkeypatch.setattr(rec, "backoff_for", lambda n: (_ for _ in ()).throw(_Stop()))
    settings = nc.settings(nc.parse("interface = eth\nwifi.ssid = N\n"), UID)
    monkeypatch.setattr(rec, "_read_settings", lambda: ({}, settings))
    with pytest.raises(_Stop):
        rec.run(_Cfg)
    assert seen == ["eth"]                                  # wifi was never brought up


def test_the_firmware_stamps_the_board_recovery_reports(make_project, monkeypatch):
    """With no image there is no system.json; the board the registration gate keys on comes
    from _ota_config, and it is the same name system.json carries."""
    from openmv_ota.build import firmware as fw

    from .test_firmware import _fake_make
    monkeypatch.setattr(fw, "_run_make", _fake_make(["bin/firmware.bin"]))
    root, repo, _app = make_project(ota=True)
    r = fw.build_firmware(root, firmware=repo, keep_build_dir=True)[0]
    ns = {}
    exec((r.build_dir / "_ota_config.py").read_text(), ns)  # noqa: S102 (generated code)
    assert ns["BOARD"] == "OPENMV_N6"

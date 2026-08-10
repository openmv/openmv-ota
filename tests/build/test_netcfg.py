"""The recovery network settings file: parsing, PSK obfuscation, and what counts as usable.

This is the one file a human edits by hand, on the one path that exists because everything
else already failed. So the tests lean on the awkward inputs a real person produces -- inline
comments, stray whitespace, a password they just typed in the clear, a half-filled static
config -- rather than on the shape the writer emits.
"""

from __future__ import annotations

from openmv_ota.build.device import openmv_netcfg as nc

UID = b"\x01\x02\x03\x04\x05\x06\x07\x08"


def test_parse_handles_what_a_person_actually_types():
    text = """
# OpenMV recovery network settings
interface   = wifi        # wifi | eth
wifi.ssid   = My Network        # spaces are legal in an SSID
WIFI.PSK    = hunter2

ipv4        = dhcp
not a setting
# ipv4.address = 192.168.1.50
"""
    cfg = nc.parse(text)
    assert cfg["interface"] == "wifi"
    assert cfg["wifi.ssid"] == "My Network"      # inner spaces kept, trailing comment dropped
    assert cfg["wifi.psk"] == "hunter2"          # keys are case-insensitive
    assert cfg["ipv4"] == "dhcp"
    assert "not a setting" not in cfg            # a line with no '=' is skipped, not fatal
    assert "ipv4.address" not in cfg             # commented out means absent


def test_parse_keeps_unknown_keys():
    """A setting a newer firmware understands must not make an older reader reject the file --
    the cost of being strict here is a device that will not come back."""
    assert nc.parse("future.thing = 1\n")["future.thing"] == "1"


def test_obfuscation_round_trips_and_hides_the_plaintext():
    out = nc.obfuscate("hunter2", UID)
    assert out.startswith("enc:") and "hunter2" not in out
    assert nc.deobfuscate(out, UID) == "hunter2"
    assert nc.is_obfuscated(out) and not nc.is_obfuscated("hunter2")


def test_obfuscation_is_keyed_on_the_device():
    """Copying the file between two boards should not carry the password across -- not because
    that is an attack worth stopping, but because it makes the obfuscation's scope honest."""
    assert nc.deobfuscate(nc.obfuscate("hunter2", UID), b"\x09" * 8) != "hunter2"


def test_deobfuscate_accepts_a_freshly_typed_password():
    # the whole point of the file being editable: type it in the clear, it works immediately
    assert nc.deobfuscate("hunter2", UID) == "hunter2"


def test_a_corrupt_obfuscated_value_degrades_instead_of_raising():
    """On the recovery path an exception is the worst outcome. A mangled value should read as
    the wrong password -- something a person can diagnose -- not as a crash."""
    assert nc.deobfuscate("enc:not-hex", UID) == "enc:not-hex"
    assert nc.deobfuscate("enc:ff", UID) is not None      # decodes, but not to valid utf-8


def test_settings_defaults_to_wifi_when_an_ssid_is_present():
    s = nc.settings(nc.parse("wifi.ssid = Net\nwifi.psk = pw\n"), UID)
    assert s["interface"] == "wifi" and s["ssid"] == "Net" and s["psk"] == "pw"
    assert s["ipv4"] == "dhcp"                            # the sane default


def test_settings_reads_an_obfuscated_psk_back():
    text = "wifi.ssid = Net\nwifi.psk = %s\n" % nc.obfuscate("hunter2", UID)
    assert nc.settings(nc.parse(text), UID)["psk"] == "hunter2"


def test_settings_returns_none_when_there_is_nothing_usable():
    """None is a real answer -- the caller falls back to DHCP on a wired interface, which is
    right for a board on a bench with a cable and no configuration at all."""
    assert nc.settings({}, UID)["interface"] == "eth"     # no ssid -> wired
    assert nc.settings({"interface": "wifi"}, UID) is None        # wifi with no network
    assert nc.settings({"interface": "carrier-pigeon"}, UID) is None


def test_static_ipv4_needs_every_field():
    """Falling back to DHCP on a half-filled static config would look like a plain failure on a
    network with no DHCP server, with nothing pointing at the real cause."""
    base = "interface = eth\nipv4 = static\n"
    assert nc.settings(nc.parse(base + "ipv4.address = 10.0.0.5\n"), UID) is None
    full = base + ("ipv4.address = 10.0.0.5\nipv4.netmask = 255.255.255.0\n"
                   "ipv4.gateway = 10.0.0.1\n")
    s = nc.settings(nc.parse(full), UID)
    assert (s["address"], s["netmask"], s["gateway"]) == ("10.0.0.5", "255.255.255.0", "10.0.0.1")


def test_render_round_trips_through_parse():
    cfg = {"interface": "wifi", "wifi.ssid": "Net", "wifi.psk": nc.obfuscate("pw", UID),
           "ipv4": "dhcp"}
    text = nc.render(cfg)
    assert nc.parse(text) == cfg
    assert "obfuscated, NOT encrypted" in text            # the honesty is in the file itself
    assert nc.settings(nc.parse(text), UID)["psk"] == "pw"


def test_a_hash_inside_a_password_is_not_a_comment():
    """'#' is a common password character. Splitting on every '#' -- the obvious parser -- eats
    the rest of the password, and the failure presents as "the right password does not work" on
    the one path that exists because everything else already failed. A comment is a '#' at the
    start of a line or after whitespace; anything else is data."""
    cfg = nc.parse("wifi.psk = pa#ss   # the real comment\n")
    assert cfg["wifi.psk"] == "pa#ss"
    assert nc.parse(nc.render({"wifi.psk": "pa#ss"}))["wifi.psk"] == "pa#ss"   # round-trips
    assert nc.parse("   # a fully commented line\nx = 1\n") == {"x": "1"}
    assert nc.parse("wifi.psk = ##\n")["wifi.psk"] == "##"

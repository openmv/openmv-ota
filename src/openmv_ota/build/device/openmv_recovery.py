"""Firmware-resident recovery -- what runs when no slot is bootable.

This is what the factory image used to be, minus the flash cost. In v1 a device that could not
mount any slot fell back to a permanent golden image whose only job was to run the OTA flow; a
whole image of flash reserved against a rare event, and useless on boards whose ROMFS is a
single erase sector. v2 puts the flow itself in the firmware, where a bad update cannot erase
it, and reserves nothing.

**It is the only thing standing between a device and a bench visit**, so its rules are
different from the update path's:

* It never gives up. There is nothing else to fall back to, so it retries forever, backing off
  so a fleet that comes up against a dead server does not hammer it.
* It assumes nothing is mounted. ``/rom`` is gone or unbootable -- that is why it is running --
  so it uses only frozen modules and the constants stamped into ``_ota_config``.
* It is allowed to be slow. A recovering device is already out of service; a careful retry that
  takes a minute costs nothing next to a wrong one that bricks it.

The pure logic here (the retry policy, the interface plan) is host-tested; the device entry
that touches ``network`` and the installer is exercised on hardware, like every other device
entry in this tree.

RAM BUDGET: this module runs inside your application, so its memory is your memory. Every
buffer here has a ceiling. Nothing is sized by a file's length, a response body, a length field
off the wire, or a queue that grows while the network is down: reads use bounded windows of a
few KB, anything larger is streamed, and large data is aliased with memoryview/bytearray_at
rather than copied.
"""

try:
    from openmv_log import log
except ImportError:                    # host / tests / a build without logging
    class _NullLog:
        def debug(self, msg, *a):
            pass

        def info(self, msg, *a):
            pass

        def warning(self, msg, *a):
            pass

        def error(self, msg, *a):
            pass

        def critical(self, msg, *a):
            pass

    log = _NullLog()

# Backoff between attempts, seconds. Starts quick -- most recoveries are a transient server or
# a router that had not finished booting -- then stretches out so a fleet that comes up against
# a genuinely dead server settles into a slow poll rather than a stampede. It CAPS rather than
# growing forever: a device that has been down for a day should still notice the fix within
# minutes of it landing, because by then someone is waiting for it.
BACKOFF_S = (5, 15, 60, 300, 900)


def backoff_for(attempt):
    """Seconds to wait before attempt number ``attempt`` (0-based)."""
    if attempt < 0:
        attempt = 0
    return BACKOFF_S[attempt] if attempt < len(BACKOFF_S) else BACKOFF_S[-1]


def interface_plan(settings, has_wifi, has_eth):
    """The interfaces to try, in order, from the parsed settings and what the board HAS.

    Returns a list of ``"wifi"``/``"eth"``, possibly empty. Two judgements are baked in:

    * **The configured interface goes first, but is not the only one tried.** A device whose
      stored credentials are stale is exactly the device that is stranded; if it also happens
      to have a cable plugged in, trying that costs one attempt and saves a bench visit.
    * **No settings at all means try wired.** A board on a desk with an Ethernet cable and no
      configuration is the common bench case, and DHCP on it needs nothing from the user."""
    order = []
    if settings:
        order.append(settings["interface"])
    if has_eth and "eth" not in order:
        order.append("eth")               # the zero-configuration option, always worth a try
    if settings and settings["interface"] == "eth" and has_wifi and settings.get("ssid"):
        order.append("wifi")
    return [i for i in order if (i == "wifi" and has_wifi) or (i == "eth" and has_eth)]


# Mirror of openmv_netcfg._OBFUSCATED. Duplicated rather than imported because the frozen
# modules are flat on-device and this file must stay importable on the host for its logic to be
# tested; test_recovery pins the two together, the same way boot.py's marker constants are.
_OBFUSCATED = "enc:"


def should_rewrite_psk(cfg):
    """Whether the stored file holds a plaintext PSK that should be rewritten obfuscated.

    Rewriting rather than deleting: the file is exactly what is needed NEXT time, and silently
    removing someone's configuration is surprising. This gets the hygiene without destroying
    the capability -- and it only writes when there is something to change, so the flash wear
    and the crash window both stay near zero."""
    psk = cfg.get("wifi.psk", "")
    return bool(psk) and not psk.startswith(_OBFUSCATED)


# --- device entry -----------------------------------------------------------
# Touches network/vfs/machine and the frozen installer; exercised on hardware.

def _uid():  # pragma: no cover  (device)  # hil-residual-fn: recovery has NO HIL scenario yet -- reaching it needs a board with both slots deliberately unbootable, which the catalog cannot do without a reflash step; it lands with the recovery bench work
    import machine
    try:
        return machine.unique_id()  # hil-residual: bare return of the hardware UID
    except AttributeError:  # hil-residual: no unique_id on this port (every real board has one)
        return b"openmv"  # hil-residual: constant fallback so obfuscation still round-trips


def _read_settings():  # pragma: no cover  (device)  # hil-residual-fn: see _uid -- no recovery scenario on the bench yet; the parsing this wraps is host-tested in test_netcfg
    """The stored settings, or ``None``. Never raises: a missing or unreadable file is the
    normal case on a board that has never been configured."""
    import openmv_netcfg as netcfg

    for path in (netcfg.PATH, netcfg.BACKUP):
        try:
            with open(path) as f:
                text = f.read(netcfg.MAX_BYTES)  # ram-ok: bounded by MAX_BYTES, not the file
        except OSError:  # hil-residual: no credentials file (the unconfigured case)
            continue  # hil-residual: try the backup copy
        cfg = netcfg.parse(text)
        log.info("recovery: read network settings")
        return cfg, netcfg.settings(cfg, _uid())  # hil-residual: bare return of the parsed pair
    log.warning("recovery: no network settings on /flash")
    return {}, None  # hil-residual: bare return (unconfigured -> wired DHCP is still tried)


def _rewrite_psk(cfg):  # pragma: no cover  (device)  # hil-residual-fn: see _uid -- no recovery scenario yet; the decision to rewrite and the obfuscation are host-tested
    """Store the PSK obfuscated, writing the BACKUP first so a crash costs one copy not both."""
    import openmv_netcfg as netcfg

    cfg = dict(cfg)
    cfg["wifi.psk"] = netcfg.obfuscate(cfg["wifi.psk"], _uid())
    text = netcfg.render(cfg)
    for path in (netcfg.BACKUP, netcfg.PATH):
        try:
            with open(path, "w") as f:
                f.write(text)
        except OSError as e:  # hil-residual: /flash is not writable (corrupt FAT); recovery continues regardless
            log.warning("recovery: could not rewrite %s (%r)" % (path, e))
            return  # hil-residual: bail out, leaving the plaintext -- never fatal
    log.info("recovery: stored the network password obfuscated")


def _bring_up(kind, settings, static=False):  # pragma: no cover  (device)  # hil-residual-fn: see _uid -- no recovery scenario yet; the interface ORDER it is handed is host-tested in test_recovery
    """Bring up one interface and wait for an address. Returns True when it is up.

    ``static`` says whether the stored IPv4 settings apply to THIS interface. Credentials
    always travel (wifi cannot associate without them); the static address does not, because
    an address written for the wired network is wrong on the wireless one and would strand a
    device that had a perfectly good DHCP server waiting for it."""
    import time

    import network
    if kind == "eth":
        nic = network.LAN()
        nic.active(True)
    else:
        nic = network.WLAN(network.STA_IF)   # CONSTRUCTED, not reused: this is what resets a
        nic.active(True)                     # wedged chip (see openmv_ota.run's recover hook)
        nic.connect(settings["ssid"], settings["psk"])
    if static and settings and settings.get("ipv4") == "static":
        nic.ifconfig((settings["address"], settings["netmask"],
                      settings["gateway"], settings["gateway"]))
    for _ in range(300):                     # ~30 s; a cold router can be slow to hand out a lease
        if nic.isconnected():
            log.info("recovery: %s up" % kind)
            return True  # hil-residual: bare return once the link is up
        time.sleep_ms(100)
    log.warning("recovery: %s did not come up" % kind)
    return False  # hil-residual: bare return (this interface failed; the caller tries the next)


def run(cfg):  # pragma: no cover  (device: the whole point is that nothing else is running)  # hil-residual-fn: see _uid -- no recovery scenario yet; the retry policy is host-tested
    """Recover a device with no bootable slot. **Never returns** -- it installs and reboots, or
    keeps trying.

    Called from ``boot.py`` when every slot was rejected. There is no fallback below this, so
    an exception anywhere in here would leave a board that only a bench can fix; every step is
    therefore wrapped, and the loop simply goes round again."""
    import time

    log.error("recovery: no bootable slot -- entering firmware-resident recovery")
    if not getattr(cfg, "SERVER_URL", ""):
        # Nothing to recover FROM. Say so loudly and stop rather than spinning: this is a build
        # mistake (no server_url in the project config), and no amount of retrying fixes it.
        log.critical("recovery: no SERVER_URL stamped in the firmware -- cannot recover")
        return  # hil-residual: terminal build-misconfiguration path (inject-only)

    attempt = 0
    while True:
        try:
            stored, settings = _read_settings()
            if should_rewrite_psk(stored):
                _rewrite_psk(stored)
            order = interface_plan(settings, _has(cfg, "wifi"), _has(cfg, "eth"))
            for kind in order:
                # Credentials always travel; the STATIC ADDRESS only applies to the interface
                # it was written for. Passing None for a fallback wifi attempt was the earlier
                # shape and it crashed on settings["ssid"] -- wifi cannot associate without
                # credentials, so the thing to withhold is the address, not the whole config.
                if not _bring_up(kind, settings, static=(kind == settings_kind(settings))):
                    continue
                log.info("recovery: installing from %s" % cfg.SERVER_URL)
                _install(cfg)                # reboots on success
            if not order:
                log.error("recovery: no usable interface")
        except Exception as e:  # hil-residual: recovery must never die -- there is nothing below it
            log.error("recovery: attempt failed (%r)" % e)
        wait = backoff_for(attempt)
        attempt += 1
        log.info("recovery: retrying in %ds" % wait)
        time.sleep(wait)


def settings_kind(settings):
    """The interface the stored settings describe, or ``None``. Used so a fallback interface is
    brought up with DHCP rather than with credentials meant for a different one."""
    return settings["interface"] if settings else None


def _has(cfg, kind):  # pragma: no cover  (device)  # hil-residual-fn: see _uid -- a capability probe, and no recovery scenario yet
    """Whether this board has an interface of ``kind``. Read from the firmware rather than
    guessed, so a board without WiFi does not spend attempts on it."""
    import network
    if kind == "eth":
        return hasattr(network, "LAN")  # hil-residual: bare capability probe
    return hasattr(network, "WLAN")  # hil-residual: bare capability probe


def _install(cfg):  # pragma: no cover  (device)  # hil-residual-fn: see _uid -- delegates to the installer, whose own paths ARE witnessed by every install scenario
    """Run the frozen installer against the build-stamped server."""
    import openmv_installer

    openmv_installer.run(cfg.SERVER_URL, getattr(cfg, "CA_PEM", b""), cfg)

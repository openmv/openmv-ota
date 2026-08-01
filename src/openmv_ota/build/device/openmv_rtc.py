"""``openmv_rtc`` -- a wall clock you can decide whether to trust.

The device records data long before it necessarily knows what time it is. This
module answers one question: *is the current time real?*, and gives you a Unix
timestamp when the answer is yes.

    import openmv_rtc

    openmv_rtc.resolve()            # once, after the network is up
    if openmv_rtc.trusted():
        ts = openmv_rtc.now()       # Unix seconds (UTC), real

HOW TRUST IS DECIDED: the firmware cannot have been running before it was built,
so a clock reading earlier than the build timestamp is provably wrong. That is
the whole test -- no network needed to apply it. A board with a coin cell (or one
waking from deep sleep, where the RTC keeps counting) is therefore trusted
immediately, with no NTP round trip; a board that cold-booted with a dead RTC
reads January 1st of the epoch year, fails the test, and stays untrusted until
:func:`sync` succeeds.

The check is LATCHED, because the RTC counts monotonically: a dead clock left
running long enough would eventually count up past the build and a bare window
test would start believing it (though the reading is really epoch-plus-uptime).
So one out-of-window reading marks the clock bad until it is actually re-set --
a clock that has ever looked wrong is never trusted just because it later looks
right.

WHY AN UNTRUSTED CLOCK REPORTS NOTHING: a wrong timestamp is worse than no
timestamp, because nothing downstream can tell it is wrong. When the clock is
untrusted, records carry only ``(sid, seq)`` and the server falls back to its own
arrival time. ``seq`` is always the exact ordering; a timestamp is a convenience
laid over it.

PORTABILITY -- both of these differ across the ports OpenMV ships, so neither is
assumed:

* ``machine.RTC`` exposes only ``datetime()`` on every port (stm32 also has
  init/calibration/wakeup, alif and mimxrt have alarm, mimxrt has irq, and NO
  port here has ``memory()``), so ``datetime()`` is the only API used.
* The epoch is **not** the same everywhere: alif and mimxrt count from 1970,
  stm32 and rp2 from 2000. ``time.time()`` on an AE3 and an N6 therefore differ
  by 30 years. The offset is detected at import from ``time.gmtime(0)`` rather
  than hardcoded, and everything this module returns is Unix (1970) seconds.

RAM BUDGET: this module runs inside your application, so its memory is your
memory. It holds a few integers and allocates only during a sync.
"""

import time

try:                                  # the firmware freezes openmv_log beside this module
    from openmv_log import log        # the shared logger -> the coverage side-channel UART
except ImportError:                   # host / a non-OTA firmware: a plain, unconfigured logger
    import logging
    log = logging.getLogger("openmv_ota")

try:                                  # the build stamps this into _ota_config
    from _ota_config import BUILD_TIME
except ImportError:                   # host, or a non-OTA firmware: no floor
    BUILD_TIME = 0

# Seconds between the two epochs MicroPython ports use (1970-01-01 -> 2000-01-01).
_EPOCH_2000 = 946684800

# How far ahead of the build a clock may read before we call it broken. A real
# device can legitimately run for years, so this is deliberately generous -- it
# only catches a wildly-wrong future reading (a corrupt RTC latching all ones).
_MAX_AHEAD = 20 * 365 * 24 * 3600

_source = "none"                      # "rtc" | "ntp" | "none"
_bad = False                          # latched: an out-of-window reading was seen


def _epoch_offset():
    """Seconds to add to this port's ``time.time()`` to get Unix time. Detected,
    never assumed: ``time.gmtime(0)`` reports the port's own epoch year."""
    return _EPOCH_2000 if time.gmtime(0)[0] == 2000 else 0


def now():
    """The current time as Unix (1970) seconds, whatever the port's epoch. Always
    returns a number -- call :func:`trusted` to find out if it means anything."""
    return time.time() + _epoch_offset()


def _in_window(unix):
    """True if ``unix`` reads at or after the build and not absurdly far past it.
    Pure -- the window arithmetic, without the session latch :func:`trusted`
    applies over it."""
    return BUILD_TIME <= unix <= BUILD_TIME + _MAX_AHEAD


def trusted():
    """True only when the clock is one we can believe: it reads inside the window
    now AND has never read outside it this session (unless re-set since).

    THE LATCH IS THE POINT. The RTC counts monotonically, so a board that cold-
    boots with a dead RTC starts near the epoch -- far below the build -- but
    left running long enough it would eventually *count up past* the build, and a
    bare window check would then believe a time that is really epoch-plus-uptime.
    So a single out-of-window reading marks the clock bad for the rest of the
    session; only setting it from a real source (:func:`set_time`, which NTP sync
    calls) clears that. A clock valid from its very first reading -- one that
    survived deep sleep, or a coin cell -- is trusted with no sync.

    With no build stamp (a non-OTA firmware) there is no floor, so the clock is
    reported untrusted rather than assumed good."""
    global _bad
    if not BUILD_TIME:
        return False
    if not _in_window(now()):
        _bad = True                   # one bad reading and we stop believing it
        return False
    return not _bad


def source():
    """Where the current time came from: ``"rtc"`` (already valid at boot, e.g.
    kept across deep sleep), ``"ntp"`` (synced this boot), or ``"none"``."""
    return _source


def timestamp():
    """The Unix timestamp to attach to a record, or None when the clock is not
    trustworthy -- callers put a ``ts`` field on a record only when this returns
    a number, so a known-bad time is never recorded as if it were real."""
    return now() if trusted() else None


def set_time(unix_s):
    """Set the RTC from Unix seconds. Uses ``RTC().datetime()``, the only setter
    available on every port; the tuple is ``(year, month, day, weekday, hour,
    minute, second, subseconds)`` with weekday 1-7.

    Setting the clock is a known-good time, so it clears the bad-reading latch --
    this is how an NTP sync rescues a clock that :func:`trusted` had given up on."""
    global _bad
    import machine
    tm = time.gmtime(int(unix_s) - _epoch_offset())
    machine.RTC().datetime((tm[0], tm[1], tm[2], tm[6] + 1, tm[3], tm[4], tm[5], 0))
    _bad = False


_NTP_HOST = "pool.ntp.org"
_NTP_DELTA_1900_1970 = 2208988800     # seconds between the NTP (1900) and Unix (1970) epochs
_NTP_WRAP_FLOOR = 3913056000          # 2024-01-01 as an NTP (1900-epoch) count; a transmit timestamp
#                                       below it means the 32-bit counter wrapped (the 2036 rollover),
#                                       so add 2**32 -- the same Y2036 fix the ntptime lib applies.
# Well-known public NTP servers to fall back to by IP when the configured host is unreachable -- a
# router or captive portal that BLOCKS or HIJACKS NTP DNS (e.g. redirecting *.ntp.org to a host that
# isn't an NTP server) makes pool.ntp.org resolve but never answer, and ntptime.time() just hangs. A
# real deployment resolves the host normally and never reaches these; they only harden the clock.
# Stable anycast (Google, Cloudflare) + fixed NIST addresses, so no DNS is needed to use them.
_NTP_FALLBACK = (
    "216.239.35.0",     # time1.google.com   (anycast)
    "216.239.35.4",     # time2.google.com   (anycast)
    "162.159.200.1",    # time.cloudflare.com (anycast)
    "162.159.200.123",  # time.cloudflare.com (anycast, alt)
    "129.6.15.28",      # time-a-g.nist.gov
    "129.6.15.30",      # time-d-g.nist.gov
)


def _ntp_query(addr, socket, struct):  # pragma: no cover  (device: network)
    """One SNTP round-trip to ``addr`` -- returns a Unix (1970) timestamp, or raises on
    timeout/error. Mirrors the ntptime lib's query (48-byte client packet, transmit timestamp at
    bytes 40:44 as seconds-since-1900, Y2036 wrap fix) with ONE change: ``recvfrom`` instead of
    ``recv``. A bare ``recv`` on an unconnected UDP socket faults on the WINC1500 (and ``connect``
    is TCP-only there); ``recvfrom`` is the portable UDP-client read and works on every stack."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(4)                               # a bit above ntptime's 1 s -> tolerate the WINC
        q = bytearray(48)
        q[0] = 0x1b                                    # LI=0, VN=3, Mode=3 (client)
        s.sendto(q, addr)
        msg = s.recvfrom(48)[0]                        # recvfrom, NOT recv (WINC); bounded 48-byte reply
    finally:
        s.close()
    val = struct.unpack("!I", msg[40:44])[0]          # transmit timestamp, seconds since 1900
    if val < _NTP_WRAP_FLOOR:                          # below 2024 -> the 2036 32-bit counter wrapped
        val += 0x100000000                             # (ntptime's Y2036 fix)
    return val - _NTP_DELTA_1900_1970                  # NTP (1900) -> Unix (1970)


def sync(host=None):  # pragma: no cover  (device: network + RTC)
    """Set the clock from NTP. Returns True on success, False if every server was unreachable -- a
    failed sync is a normal state to retry, never an exception for the caller to handle.

    Follows the ntptime lib's query (see ``_ntp_query``) with two robustness changes: ``recvfrom``
    instead of ``recv`` (a bare recv faults on the WINC1500's UDP socket), and a FALLBACK LIST. The
    configured host is tried first (a real deployment resolves ``pool.ntp.org`` and stops there),
    then a set of well-known anycast NTP servers by IP -- so the clock still syncs on a network that
    blocks or hijacks NTP DNS, where ntptime.time() would simply hang."""
    global _source
    import socket
    import struct
    targets = []
    try:
        targets.append(socket.getaddrinfo(host or _NTP_HOST, 123)[0][-1])   # configured host (via DNS)
    except Exception:  # hil-residual: DNS down (name resolution failed) -> use the fixed IP fallbacks
        pass  # hil-residual: bare pass; the IP fallbacks below need no DNS
    for ip in _NTP_FALLBACK:
        targets.append((ip, 123))
    for addr in targets:
        try:
            unix = _ntp_query(addr, socket, struct)
        except Exception:  # hil-residual: this server timed out / was blocked -> try the next one
            continue  # hil-residual: bare continue
        if unix < BUILD_TIME:                          # a reply older than the build is bogus (an unset
            continue  # hil-residual: bare continue    # server clock, or a non-NTP host answering) -> next
        set_time(unix)
        _source = "ntp"
        log.debug("clock: ntp synced")                 # HIL path witness (NTP query set the RTC)
        return True  # hil-residual: bare return (NTP sync ok)
    return False  # hil-residual: bare return (all servers failed -> retry next poll)


def resolve(host=None):  # pragma: no cover  (device: network + RTC)
    """Establish the clock once, cheaply: keep what the RTC already has if it is
    trustworthy (the deep-sleep and coin-cell case -- no network needed), else
    try one NTP sync. Returns True if the clock ended up trustworthy.

    Safe to call repeatedly: once the clock is good it costs a comparison."""
    global _source
    if trusted():
        if _source == "none":
            _source = "rtc"  # hil-residual: bare assign (RTC trusted at boot without NTP; bench NTP-syncs first)
        log.debug("clock: rtc trusted")           # HIL path witness (fast path: clock already good)
        return True  # hil-residual: bare return (clock trusted)
    log.debug("clock: syncing")                   # HIL path witness (untrusted -> one NTP sync)
    return sync(host)  # hil-residual: tail call to sync() (its NTP path is witnessed by clock: ntp synced)

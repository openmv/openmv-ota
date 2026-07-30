# Production main.py — OTA updates + a deep-sleep-safe watchdog, wired the way the OTA
# install path was PROVEN to survive on hardware (HIL: N6 WWDG and RT1060 WDOG, both at a
# 100 ms window). Copy this into app/main.py and fill in the four TODOs.
#
# The shape is what matters:
#   1. slow one-time setup (camera, network) runs UNWATCHED
#   2. openmv_wdt.start() arms the watchdog only AFTER that setup
#   3. openmv_ota.run() polls + installs in the background — it feeds the watchdog across
#      its OWN long flash erase/write, so an update survives even though this loop stops
#      while install() erases the slot this app runs from
#   4. a tight main loop feeds the watchdog by REAL PROGRESS and confirms once healthy
#
# The watchdog is OPT-IN: to turn it on, edit device/openmv_wdt.py -> ENABLED = True and
# rebuild firmware. With ENABLED = False (the default) every openmv_wdt call below is a
# no-op, so this file runs unchanged whether or not you enable it.

import asyncio
import logging
import sys

import network
import openmv_ota
import openmv_wdt

_log = logging.getLogger("openmv_ota")

SERVER = "https://updates.example.com"   # TODO: your update server / static host
POLL_AFTER_S = 3600                      # hourly; the server may return its own cadence


def healthy():
    """Return True once the app has proven itself ACTUALLY operational — a frame captured, a
    model loaded, a peripheral answered: whatever "working" means for your product. confirm()
    is gated on this. A weak "I reached the loop" check defeats the auto-rollback: a trial
    that boots but is broken would confirm itself and strand you on the bad image. Keep it
    quick — it runs every loop until it passes."""
    return True                          # TODO: replace with your real health check


async def do_work():
    """Your product's real work — capture, infer, actuate. Runs every loop iteration.

    Watchdog rule: keep ONE iteration shorter than the window (100 ms). If a step here can
    take longer, either subdivide it and openmv_wdt.feed() per step, or — only for a single
    op you truly can't split (a big model load) — wrap it:

        with openmv_wdt.relax():
            load_model()

    relax() feeds from a timer ISR for the op's duration, so it can only catch a total CPU
    death, not a stuck loop — use it rarely and narrowly; prefer subdivide + feed."""
    await asyncio.sleep_ms(0)            # TODO: your work here


async def bring_up_network():
    """Slow one-time setup. Runs BEFORE openmv_wdt.start(), so it is unwatched — a stuck
    network here won't reset the board (an OTA-less hang, which you'd notice)."""
    wl = network.WLAN(network.STA_IF)
    wl.active(True)
    wl.connect("SSID", "PASSWORD")       # TODO: your credentials
    while not wl.isconnected():
        await asyncio.sleep_ms(200)


async def main():
    openmv_ota.sync()                    # apply bundled coprocessor/romfs resources (no-op if none)
    await bring_up_network()             # slow setup, before the watchdog is armed

    openmv_wdt.start()                   # ARM now — past setup, about to enter the steady loop.
                                         # Arming at import would let the ~100 ms window expire
                                         # during bring-up, before the first feed(), and reset.

    # The OTA lifecycle runs concurrently (check-in, install, trial-reboot). install() feeds the
    # watchdog across its own erase/write, so an update completes even though THIS loop stops the
    # moment install() erases the FRONT slot we execute from.
    asyncio.create_task(openmv_ota.run(SERVER, poll_after_s=POLL_AFTER_S))

    confirmed = False
    while True:
        openmv_wdt.feed()                # feed by REAL PROGRESS: one feed == one loop of real work,
                                         # so a hung loop stops feeding and the board resets.
        await do_work()
        if not confirmed and healthy():
            openmv_ota.confirm()         # promote off trial (no-op unless we booted a FRONT trial)
            confirmed = True
        await asyncio.sleep_ms(20)       # cadence well under the 100 ms window


try:
    _log.info("boot")
    asyncio.run(main())
except Exception as e:                   # a crash in main() must be visible, never silent
    _log.error("app crashed: %r" % e)
    sys.print_exception(e)

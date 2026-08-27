# Logging & the watchdog

*[← 12 · The device library](12-device-library.md) · [Index](00-introduction.md) · [14 · The update server →](14-update-server.md)*

---

Two frozen **survival modules** ship in every OTA firmware beside `boot.py` —
plain Python, frozen so they exist even when the romfs doesn't: `openmv_log`
(see what the device is doing, across reboots) and `openmv_wdt` (keep it alive
when nobody is watching).

## Debug logging

On-device OTA failures are otherwise invisible — `boot.py` runs before the REPL is up,
and `install()` reboots, so neither can `print()` anywhere you'll see. So there's an
opt-in logger built on the **standard `logging` module** (frozen on every OpenMV board
via the board manifest's `require("logging")`). `boot.py`, the installer, and the runtime
lib all log to the `openmv_ota` logger; your app uses the same standard tree:

```python
import logging
logging.getLogger("openmv_ota").info("hi")     # or: openmv_ota.log.info("hi")
```

The configuration lives in `device/openmv_log.py`, scaffolded into your project and frozen by
`build firmware` as **`openmv_log`** (frozen so `boot.py` can use it before `/rom` mounts).
It's **off by default** (the logger's level is set above `CRITICAL`, so nothing emits and
nothing leaks to the REPL). To debug on hardware, edit it and rebuild firmware:

```python
ENABLED = True         # master switch
UART    = 3            # your board's machine.UART id (the port differs per board)
BAUD    = 115200       # UART = None -> log to the USB REPL instead
LEVEL   = logging.INFO # show this level and above
```

Output is kernel-style. It prefers **wall-clock UTC from the RTC** — which is set by the
time the installer runs, because TLS cert validation requires it (`ntptime.settime()`) —
and falls back to **monotonic uptime** before the clock is set (e.g. in `boot.py`):

```
[   12.345] INFO openmv_ota: boot: mounted A (payload 1)                  (RTC unset)
[2026-06-25 12:34:56] WARNING openmv_ota: install: FAILED after erase     (RTC set)
```

`boot.py` logs the mounted slot and any reject reason; the installer logs each phase
(download / erase+write / done / failure); `confirm()`/`sync()` log their actions. Any
`machine.UART` is created once and kept by the handler. Because `device/openmv_log.py` is
*yours*, sending logs elsewhere (a file, a socket) is just editing its handler — the
levels, filtering, and API are the standard `logging` ones.

## Watchdog

A real app should run a watchdog so a hang reboots the device instead of bricking it.
Like the logger, there's an opt-in helper — `device/openmv_wdt.py`, frozen as
**`openmv_wdt`**, **off by default**, yours to edit. Turn it on and rebuild firmware:

```python
ENABLED    = True   # master switch (off by default — every openmv_wdt call is then a no-op)
WDT_ID     = None   # None = auto-select the DEEP-SLEEP-SAFE watchdog for this port
TIMEOUT_MS = 100    # reset if not fed within this long — MUST be ≤ the board's WDT max
TIMER_ID   = -1     # machine.Timer id; on OpenMV ports only the soft timer (-1) exists
FEED_HZ    = 50     # relax() ISR feed rate; keep well above 1000 / TIMEOUT_MS
```

Use the **deep-sleep-safe** watchdog — the one that *stops* while the device deep-sleeps, so it
can't reset you mid-sleep. `WDT_ID = None` auto-selects it per port: the **WWDG** on stm32/N6, the
default `machine.WDT` (WDOG / alif WDT) elsewhere. The catch is that the deep-sleep-safe watchdog is
**short** — the N6 WWDG maxes at 167 ms — so this is a **tens-of-milliseconds discipline**, not
seconds. (The always-counting IWDG can run for minutes but resets a *sleeping* device; pick it only
if your app never deep-sleeps.)

### The feed contract

Five rules. The **`main.py` that `openmv-ota project new --ota` scaffolds already follows all of
them** (it arms after camera setup, feeds per captured frame, and health-gates `confirm()`), and the
OTA install path is engineered to as well — that's what lets an update complete under an armed 100 ms
watchdog, proven on N6 + RT1060 hardware:

1. **Arm after setup, not at import.** Call `openmv_wdt.start()` once — when your slow one-time
   setup (camera reset, network bring-up) is *done* and you're entering the steady loop. Arming at
   import would let the ~100 ms window expire *during* that setup, before your first `feed()`, and
   reset the board. `start()` is a no-op while the watchdog is off, so leave it in unconditionally.
2. **Feed by real progress.** `openmv_wdt.feed()` once per loop of *actual work*, so a feed means
   work happened and a stuck loop stops feeding → reset. Don't feed from a bare timer just to keep
   it quiet — that masks the exact hang you wanted to catch.
3. **Feed on a tight cadence.** Every ~10–20 ms while awake (`await asyncio.sleep_ms(20)`), well
   under the window. A coarse `sleep(2)` loop *will* reset you.
4. **Split long ops, or `relax()` them.** One loop iteration must fit the window. If a step can't
   (a big model load), subdivide it and feed per step; only as a last resort wrap a truly
   unsplittable op in `with openmv_wdt.relax():`.
5. **Boot needs no feeding.** `machine.reset()` — including the OTA trial reboot — clears the WWDG,
   so every boot runs unwatched until your app calls `start()` again. You never thread a feed
   through boot.py.

**Long blocking ops vs. the watchdog.** A multi-second flash erase (an OTA install), a
model load, etc. can't feed from the main loop and would trip the watchdog. Wrap them:

```python
with openmv_wdt.relax():
    do_long_thing()
```

`relax()` runs a `machine.Timer` whose callback feeds the watchdog at **interrupt time**,
so the board survives the op *as long as the CPU is healthy* (interrupts still firing) —
effectively suspending the watchdog without disabling it, and on exit it stops and hands
feeding back to your loop. Use it only around genuinely long ops; outside `relax()` the
watchdog still catches a hung loop. On every OpenMV port `machine.Timer` *is* the
virtual/soft timer (`-1`, the only id it accepts), and the helper creates it with
`hard=True` — that runs its callback in the SysTick/PendSV interrupt handler, which is
what lets the feed fire mid-erase. Without `hard=True` the callback is *scheduled* and
wouldn't run while the CPU is blocked, so the erase would still trip the watchdog.

**`install()` and `sync()` already do this, minimally** — each `relax()`es *only* the one
long flash erase (which it can't feed from a loop and which can exceed even the WDT's max
timeout) and `feed()`s the watchdog **per chunk** through the surrounding loops (`install`
through the download + write; `sync` through its write *and* the already-applied re-read).
So an OTA install or a `sync()` won't trip an enabled watchdog, yet a genuine stall
*isn't* masked: if a loop stops or a recv stalls, feeding stops and the watchdog resets the
board — which lands it back on the previous slot. `install()` also sets a 30 s socket timeout as the same backstop when no watchdog
is enabled (a stalled download fails cleanly instead of hanging). All a no-op if you
haven't enabled a watchdog.

---

*[← 12 · The device library](12-device-library.md) · [Index](00-introduction.md) · [14 · The update server →](14-update-server.md)*

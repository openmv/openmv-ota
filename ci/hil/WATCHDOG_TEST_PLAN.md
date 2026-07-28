# Watchdog verification plan (openmv_wdt) — RT1062 + N6

**Goal.** Prove that when a user turns the watchdog on, *all* of our device code services it
seamlessly — no long blocking operation outruns the timeout and resets the board mid-work — and
prove the watchdog actually bites when the app genuinely hangs. This closes the last non-coprocessor
residual cluster (`openmv_wdt.py`, currently `ENABLED = False` → `# hil-residual`).

The watchdog is **opt-in and off by default** (`openmv_wdt.ENABLED = False`). Turning it on is a
manual edit + firmware rebuild. So none of this changes default behaviour; it makes the *enabled*
path trustworthy.

---

## 0. The binding constraint: a sub-100 ms window (NOT seconds)

**Guiding principle — the watchdog is fed CONSTANTLY, and by REAL PROGRESS.** Not "feed before and
after a long op" — feeding is a continuous heartbeat (every few ms, well inside the window) that keeps
ticking *through* every operation. Crucially, the feed should come from **actual forward progress**,
so that a feed *means* work happened: a per-chunk loop feeds as it processes each chunk, so a logic
hang stops the loop → stops feeding → reset. That is the watchdog doing its job.

`openmv_wdt.relax()` — the timer-tick ISR feeder — exists for a single op that genuinely can't
loop-feed, but it is **effectively a temporary disable**: the ISR feeds on a timer *regardless of
whether the code is making progress*, so during `relax()` the watchdog can only catch a total CPU /
interrupt death, **not a stuck loop or a wedged driver.** So `relax()` is a last resort to **minimize
in count and duration**, not the default tool for blocking ops.

That gives a strict preference order for every blocking stretch:

1. **Best — progress-based loop feed:** restructure the op into short iterations (each ≪ window) and
   feed per iteration. A feed = progress. (per-chunk hash, per-chunk write, per-block erase, and —
   see §4 — *short non-blocking network reads in a feed loop* instead of one long blocking `recv`.)
2. **Acceptable — concurrent progress feed:** an asyncio op whose `await` yields to a live event loop
   that a *separate* progress loop is feeding (the check-in path, #6).
3. **Last resort — `relax()`:** only where a single hardware op can't be subdivided and blocks longer
   than the window (a flash *sector* erase that itself exceeds the window, §3b/§6). Wrap the smallest
   possible region, and treat every use as a known coverage/robustness gap to justify.

The audit below asks, of every path: "is the feed *continuous* and driven by *progress* here — or are
we leaning on `relax()` (a disable) and can we remove it?"

The watchdog apps will actually use is the **deep-sleep-safe** one, and it is **short**:

- **STM32 (N6):** two watchdogs. **IWDG** keeps counting *through deep sleep* (LSI-clocked), so it
  resets a device that deep-sleeps longer than its timeout — unusable for any app that sleeps. The
  **WWDG** stops in deep sleep (what apps want) but its **max timeout on the N6 is 167 ms** (hard
  ceiling), and it is a *window* watchdog (refreshing **too early** also resets). **We design to
  ~100 ms** to sit comfortably inside 167 ms. This is the tightest OTA board and sets the budget.
- **RT1062:** similar — its watchdog **stops in deep sleep** too; treat its max as TBD (§6) but design
  to the tighter N6 number.

**Everything below is re-scoped to a ~100 ms feed budget.** Design to the tightest board (N6 WWDG,
~100 ms); a board with a larger max is then trivially satisfied. The consequences are large:

- The hard part is **the network**, not the erase: a single TLS handshake / DNS / `recv` easily
  blocks 100s of ms and cannot be fed from a loop → it **must** run under `relax()` (ISR feed).
- `relax()`'s `FEED_HZ` must be **well above 10 Hz** (feed every few ms), not the current default, or
  it can't stay inside a ~100 ms *and* window-lower-bounded WWDG.
- Any flash-erase *piece* must complete (or keep interrupts live) in **< 100 ms**, so the ISR feed
  can fire — #19348's ranged erase must erase in small-enough blocks, and each block must not hold
  interrupts off for ~100 ms.
- **Which watchdog does `openmv_wdt` actually drive?** It calls `machine.WDT(WDT_ID, TIMEOUT_MS)`,
  which on stm32 MicroPython is the **IWDG** — the *wrong*, deep-sleep-continuing one. **Open item
  (§6):** confirm whether the OpenMV port exposes WWDG (via `WDT_ID`?) or whether `openmv_wdt` must be
  reworked to drive WWDG, and what a *window* watchdog means for `feed()` (don't refresh too early).
  If the only reachable watchdog is IWDG, document that clearly (deep-sleep apps can't use it).
- `TIMEOUT_MS = 5000` in the sample is a placeholder from the IWDG assumption; for WWDG it's ~100 ms
  or the board max. The sample must set a realistic value per board.

---

## 1. How openmv_wdt works (the two feeders)

`src/openmv_ota/build/device/openmv_wdt.py`:

- **`feed()`** — call from a loop you control. A no-op when the watchdog is off. This is the primary
  feeder: your main loop, and every in-loop step of our long operations, call it.
- **`relax()`** — a context manager that feeds the watchdog *from a hardware-timer ISR* (soft timer
  `-1`, `hard=True` → runs in the SysTick/PendSV handler) for the duration of a single blocking op
  the main loop can't reach. It works **as long as the CPU is healthy** (interrupts still firing).
  Use it only around a genuinely unfeedable op; outside it, the watchdog still catches a hung loop.
- The hardware watchdog starts at `openmv_wdt` **import time** when `ENABLED` (`_start()` →
  `machine.WDT(WDT_ID, TIMEOUT_MS)`). Config knobs: `WDT_ID`, `TIMEOUT_MS` (default 5000; *board max
  may be lower*), `FEED_HZ` (relax feed rate).

**Design contract we are verifying:** the app main loop feeds faster than `TIMEOUT_MS`; every long
op we ship either (a) runs inside that loop's cadence, (b) feeds per chunk itself, or (c) is wrapped
in `relax()`. Nothing blocks longer than `TIMEOUT_MS` without a feed.

---

## 2. Blocking-operation audit (the heart of this)

Every device path that can block, its worst-case window, and how it's serviced. `T` = `TIMEOUT_MS`.

| # | Path (file:line) | Worst-case block | Feed mechanism today | Status |
|---|---|---|---|---|
| 1 | **boot.py body SHA-256** — `_sha256(body[:body_size])` `boot.py:193`, loop `boot.py:151-156` | multi-MB hash, ~0.6–1.2 s per slot on M7 (×1–2 slots) | **none** — boot.py never imports/feeds openmv_wdt | ⚠ **GAP** (see §3) |
| 2 | **boot.py ECDSA verify** — `verify(...signed_region)` `boot.py:183` | header‖meta only (small) → ~ms | none needed (fast) | ✅ once boot feeds at each step |
| 3 | **FRONT erase** (install) — ranged `rom_ioctl(3,...)` `installer.py:~852-860` | one **sector** erase = 50–400 ms (indivisible) | `feed()` per erased block `installer.py:860` | ⚠ **only ✅ if one sector-erase < window** (§4b); else `relax()`-or-bust — **measure (§6)** |
| 3b | legacy single-shot erase `installer.py:870` (`with relax()`) | whole erase in one C call | `relax()` ISR feed = **a disable** | ✅ unreachable on #19348 fw (and it faults the N6) — good, since `relax()` is what we avoid |
| 4 | **image download + write loop** — `_install_stream` `installer.py:552-605`, writes `installer.py:782` | per-chunk write ~ms; **per-`recv` up to `_SOCK_TIMEOUT = 30 s`** `installer.py:95,635` | `feed()` per chunk; a blocking `recv` **can't feed** and must not be `relax()`'d wholesale | ⚠ **restructure to short progress-fed reads (§4)** |
| 5 | **delta reconstruct** — `_delta_stream` + ulab `_add` | per output chunk | fed by the write loop (#4) | ✅ |
| 6 | **check-in poll** — `run()` loop `__init__.py:460-480`, `_checkin`/`_read_capped` network awaits | TLS handshake + recv, seconds | `_wdt_feed()` per poll `__init__.py:478` **+ the app main loop feeds concurrently** (asyncio interleave) | ✅ *iff the sample main.py feeds* (§5) |
| 7 | **confirm / rollback floor** — marker writes `__init__.py`/`boot.py` | byte-granular, ms | inherently short | ✅ |
| 8 | **sync() coproc erase** — `_partition_apply` `__init__.py:609-634` | seconds | `with _wdt_relax()` `:618` + `_wdt_feed()` per chunk `:634` | ✅ (AE3-only, deferred) |
| 9 | **app code** (user main.py) | unbounded | the user's responsibility | 📄 documented contract + reference main.py (§5) |

**Two items need action: #1 (boot.py) and #4 (network-vs-WDT).** Everything else is already fed or
split. The key structural fact: the two multi-MB compute ops (boot body-hash #1, install write #4/5)
are **already chunked at 4096 bytes** — they just need `feed()` added (boot) / already have it
(install). #19348's ranged erase is precisely the "split the long op up" step for the FRONT erase.

---

## 3. boot.py plan — classify each op, feed appropriately

boot.py runs *before* the app imports `openmv_wdt`, so the WDT is normally **not armed during boot**
— **unless the hardware WDT survives `machine.reset()`** (the install→trial reboot). On STM32 the
IWDG typically *does* survive a soft reset (the WWDG resets on it); on mimxrt it's TBD (§6). If it
survives, boot.py runs its body-hash under an armed, unfed WDT → **reset loop**. So we feed boot.py
regardless, importing `openmv_wdt` with the runtime's host-safe fallback (`try: import openmv_wdt /
except ImportError: <null-feed>`) so host imports and non-OTA firmware stay inert, and `feed()` stays
a no-op when the watchdog is off (default boots unchanged).

The right approach (your steer): **walk every boot op, mark it instant or long-running — including
the ones outside loops — and feed to match.** Under a ~100 ms window, "long-running" means "could
approach or exceed the window." First pass (times to be confirmed on HW, §6):

| boot op (file:line) | class | feed |
|---|---|---|
| `vfs.rom_ioctl(2,0)` + `addressof` + `bdev` check `boot.py:324-330` | **instant** | one feed at boot entry brackets it |
| `vfs.umount("/rom")` `boot.py:362` | **instant** | — |
| `read(...)` (all of them) — `uctypes.bytearray_at` aliases, zero-copy `boot.py:334` | **instant** | none (no I/O, just a view) |
| `parse_trailer` / `_rollback_floor_of` — CRC over ONE block `boot.py:243,246` | **instant** (~sub-ms) | — |
| **`verify(...)` ECDSA over `signed_region`** (header‖meta, small) `boot.py:183` | **short, single C call** — ~tens of ms? **can't feed inside it** | feed **immediately before**; if measured > window it's a `relax()`-or-bust op (§6) |
| **`_sha256(body[:body_size])`** multi-MB, **chunked at 4096** `boot.py:151-156,193` | **LONG — but a loop** | **`feed()` inside the chunk loop** (every chunk / every Nth) — the primary boot feed |
| `write_marker(...)` — small flash program + read-back `boot.py:344-359` | **short** (one small program) | feed after |
| **`mount()` → `vfs.VfsRom(body)` + `vfs.mount`** `boot.py:340-342` | **TBD — instant if header-only, LONG if it scans/indexes the whole romfs** | **classify on HW (§6)**; if it scans, feed inside/around, else one feed after |
| `os.chdir` / `sys.path.append` `boot.py:388-390` | **instant** | final feed |

So the concrete change is small: `feed()` inside `_sha256`'s loop (the one true long op), a `feed()`
bracketing each slot attempt and right before the ECDSA verify, and a feed after mount — with two
ops flagged to **measure before trusting**: the ECDSA verify (single C call — if it alone exceeds the
window we have a problem no loop-feed can fix) and `VfsRom` mount (does it touch the whole partition?).
The `_sha256` feed line runs every boot, so it's witnessed by the existing boot markers — no new
residual, and it directly closes the boot-hash gap (audit #1).

---

## 4. The network path — the hard part under a ~100 ms window

Network is where progress-based feeding is hardest and where the pull toward `relax()` (a disable) is
strongest. The installer downloads over **blocking** sockets with `_SOCK_TIMEOUT = 30 s`
(`installer.py:95,635`): a single `recv` can block far longer than a ~100 ms window with no feed.
Wrapping the download in `relax()` would "work" but disables the watchdog across the *entire*
multi-second download — exactly what we're avoiding.

**Preferred fix (progress-based) — restructure the installer's reads into short fed iterations:**
- short per-read timeout (or non-blocking + poll); read whatever is available (≤ a few KB), `feed()`,
  loop until the chunk is complete or the overall 30 s budget is spent. Each iteration is ≪ window and
  a feed *means* "more bytes arrived."
- A dead link then stops producing bytes → the loop stops feeding → reset (the watchdog's job); or the
  overall deadline raises → retry → golden. The watchdog stays meaningful; we never `relax()` the
  whole download. More work than one blocking `recv`, but it's the only shape that keeps a ~100 ms
  watchdog honest through a multi-MB transfer.

**Check-in poll (#6):** already the right shape — `run()` is asyncio, so `await _checkin(...)` yields
to the event loop; as long as the app's progress loop (§5) feeds on that same loop, the network wait
is covered without `relax()`. Verify the interleave feeds within the window (depends on the app loop
cadence and on no single `await` parking the loop for > window).

**DNS / TLS handshake:** the one sub-step that's a single blocking call we can't easily subdivide. If
it exceeds the window it's the strongest (and maybe only) candidate for a *narrow, deliberate*
`relax()` around just the handshake — **measure it first** (§6). A handshake to a nearby server may
fit a ~100 ms window; a distant/slow one won't. If we must `relax()` here, it wraps only the handshake
(seconds, once), not the whole download, and is documented as a justified exception.

### 4b. The erase, re-examined against the window

`#19348`'s ranged erase feeds per **block** — but that only helps if a *single* block/sector erase
finishes within the window. NOR **sector-erase times can be 50–400 ms**, which can *exceed* a ~100 ms
WWDG window on its own. A single sector erase is one indivisible hardware op: you cannot loop-feed
*inside* it. So if `sector-erase-time > window`, the erase is a genuine `relax()`-or-bust case — and
even `relax()` only works if the flash driver keeps **interrupts live** during the sector erase (many
don't). **This is the sharpest open risk (§6): measure a single sector-erase time and whether IRQs
fire during it on each board.** Possible outcomes: (a) sector erase fits the window → per-block feed
is enough, no relax(); (b) it doesn't but IRQs stay live → a tightly-scoped `relax()` per sector; (c)
neither → a ~100 ms watchdog is not compatible with our in-place erase and we'd need a larger window
or a different erase strategy. We won't know which until we measure.

---

## 5. Reference `main.py` + config (the "seamless when on" deliverable)

Ship a documented sample that turns it on correctly, sized to the real window:

- `openmv_wdt.py`: `ENABLED = True`, `WDT_ID = <selects the deep-sleep-safe watchdog, §6.1>`,
  `TIMEOUT_MS = <board max — ~100 ms on N6 WWDG>`, `FEED_HZ = <fast enough for the window, §6.9>`.
- `main.py` pattern — **the app loop feeds on a tens-of-ms cadence, not a coarse one:**
  - `import openmv_wdt` early (arms the WDT).
  - **A coarse `while True: sleep(2)` loop is incompatible** — with a ~100 ms window the app must
    `feed()` every ~10–20 ms while awake. Show a tight `feed()` + short `sleep_ms()` heartbeat, or
    feed from within the app's own per-frame/per-step work.
  - **Deep sleep:** the WWDG *stops* in deep sleep (the whole reason to use it), so no feed is needed
    while asleep — the pattern is "feed tightly while awake, deep-sleep freely, resume feeding on
    wake." Show that explicitly.
  - `openmv_ota.run(...)` as an asyncio task: its network awaits are covered by the app loop feeding
    on the same event loop — **not** by `relax()`. The sample must make the loop cadence obviously
    faster than the window.
  - App-side long ops: prefer subdividing + per-step feed; `with openmv_wdt.relax():` only as the
    documented last resort (it's a temporary disable).
- **Contract** at the top of `openmv_wdt.py` / the sample: *the deep-sleep-safe watchdog window is
  short (~tens of ms); your loop must feed within it while awake; deep sleep stops it; subdivide long
  work and feed per step; `relax()` disables it — use sparingly. The install and boot paths already
  service it.*

---

## 6. Hardware unknowns — resolve experimentally (Part B, needs the bench)

None of these are knowable from source; each is a small standalone script per board (RT1062, N6).
Read `docs.openmv.io` / the OpenMV MicroPython port + `machine.WDT`/`machine.WWDG` first (our "read
the device docs before relying on an API" rule). In rough priority:

1. **Which watchdog is reachable — RESOLVED by two upstream PRs (not in our fork yet):**
   - **micropython#19350** (STM32 WWDG): adds `machine.WDT("WWDG")` (IWDG stays the default
     `machine.WDT()`/`WDT(0)`; also `"IWDG2"`/`"WWDG2"` on dual-core H7). WWDG **stops in deep sleep**
     (what apps want). **The N6's WWDG max is 167 ms** — that is the hard ceiling for an OTA board, and
     the source of our ~100 ms design target (comfortably inside 167 ms). (The tiny F4/F7 WWDG maxes,
     38–49 ms, are irrelevant — those boards never run the OTA system.) So `openmv_wdt` must select by
     name (`WDT_ID = "WWDG"` on N6), not `0`.
   - **micropython#19399** (ALIF WDT): adds `machine.WDT` to the AE3 (alif), **~100 ms – 10.7 s**,
     **off in deep sleep** (NMI→reset shim). More generous than N6's WWDG.
   - **mimxrt (RT1062):** confirm its `machine.WDT` (WDOG) max and that deep sleep stops it (user: it
     does). Likely the *loosest* window of the three.
   - **Consequence:** `openmv_wdt` needs rework to select the deep-sleep-safe watchdog **per board**
     (`"WWDG"` on N6, default on AE3/RT), and **both PRs must be cherry-picked into our micropython
     fork first** — same mechanism as #19348 (`project._ensure_ota_firmware_features`). Until then,
     `machine.WDT` on the N6 is only the (unusable-for-deep-sleep) IWDG.
   - **Binding design point:** N6's WWDG is the tightest window (tens of ms) → **design to it**; AE3
     and RT1062 (100s of ms – seconds) are then easily satisfied.
2. **WWDG max + window** (N6): confirm the ~100 ms ceiling and the *window lower bound* (feeding too
   early also resets) — that sets both `TIMEOUT_MS` and the legal feed cadence. RT1062: its watchdog's
   max + confirm deep sleep stops it.
3. **Survives `machine.reset()`?** Arm, `machine.reset()`, check whether the next boot is already on
   the clock. Per watchdog (IWDG likely yes; WWDG likely no). Sets how urgent boot.py feeding is
   (we feed regardless, §3).
4. **Single flash SECTOR-erase time + do IRQs fire during it** (the FRONT slot flash, each board). The
   pivot for §4b: if one sector erase < window → per-block feed suffices; if not but IRQs stay live →
   tightly-scoped `relax()` per sector; if neither → a ~100 ms watchdog is incompatible with in-place
   erase. **Highest-leverage measurement.**
5. **ECDSA verify time** — time `ecdsa_verify.verify(...)` over a real trailer's `signed_region`
   (single C call) vs the window. If it alone exceeds the window, boot's verify is a `relax()`-or-bust
   op (§3).
6. **`VfsRom` mount time** — mount a multi-MB romfs; does it scan/index the whole partition (long →
   needs feeding) or just parse a header (instant)? Classifies the boot `mount()` row (§3).
7. **TLS handshake time** to the bench server vs the window (§4 handshake decision).
8. **`machine.Timer(-1, hard=True)` ISR feed actually fires during a blocking op** — during a sector
   erase and during a blocked `recv` — since that's the entire premise of `relax()`. And is it usable
   in the frozen boot path (only matters if §5/§6.5/§6.6 force `relax()` into boot).
9. **`FEED_HZ` for the window** — the ISR/loop feed rate that stays *inside* a ~100 ms WWDG *and* not
   too early. The current default (10 Hz) is almost certainly too slow for a windowed 100 ms WWDG.

---

## 7. HIL scenario — prove **both** directions

A WDT-enabled firmware variant + a new scenario in `ci/hil/ota_cycle.py` (`SCENARIOS`),
reliable-boards-only (N6 + RT1062; AE3 stays out while its USB/DFU is debugged).

1. **`watchdog` (positive / survives).** Build firmware with `ENABLED = True`, run the full delta
   cycle — golden → offer → **the multi-second FRONT erase** → write → trial → confirm → promote —
   plus steady-state polling. **Pass = the device completes the cycle with no spurious reset.** This
   proves every long op (#1, #3, #4, #5, #6) is fed/split under a live watchdog. New marker asserting
   "reached confirm.promoted without a watchdog reset."
2. **`watchdog_bite` (negative / actually resets).** A bench-app variant that **stops feeding** (a
   busy-wait > `TIMEOUT_MS` with no `feed()`/`relax()`). **Pass = the device resets** (WDT is real,
   not a no-op). Detect via `machine.reset_cause()` (`WDT_RESET`) reported on the next boot, and/or
   the boot markers reappearing after the deliberate hang. This is the check that the whole feature
   isn't silently dead.
3. **`boot.py under WDT` (only if §6.1 = survives).** After the app arms the WDT and reboots into the
   trial, assert boot.py survives its body-hash — i.e. §3's boot feeding works. Falls out of scenario
   1 once §6.1 is known (the trial reboot already happens there).

**Safety:** run the first enabled build with a deliberately generous `TIMEOUT_MS` (e.g. board max) so
a mistake doesn't reset-loop a board off the bench; tighten to 5 s once the paths are proven. Keep a
recovery path (re-flash a `ENABLED = False` golden via blhost/DFU) — a reset-looping board is painful
to recover, so this is the highest-risk experiment.

---

## 8. Coverage closure (Part E)

The `watchdog` scenario exercises the `openmv_wdt` device lines currently carrying
`# hil-residual: ...covered by a future watchdog-enabled HIL scenario` — `feed()` body, `_tick`,
`_Relax.__enter__/__exit__`, `_start`. Once the scenario runs on HW:
- add the marker(s) to `ci/hil/ota_cycle.COVERAGE` + the `watchdog` scenario's `expect`;
- flip those residuals to witnessed in `openmv_wdt.py` (or keep any path that still can't run as a
  residual with an honest reason);
- `tests/hil/test_device_coverage_audit.py` then proves the watchdog module is fully accounted — the
  **last non-coproc residual cluster closed.**

---

## 9. Sequencing & effort

The hardware measurements (§6) now **gate the design** — several decide whether a ~100 ms watchdog is
even compatible with our flows — so measure before committing device changes.

1. **Part A (this doc) — done.** The audit + the shape of the changes. No hardware.
2. **Part B (§6) — next, it gates everything.** Which watchdog is reachable (§6.1), the WWDG window
   (§6.2), and the **sector-erase-vs-window** pivot (§6.4). Small standalone scripts; needs the bench
   free.
3. **Part A′ — device changes, informed by B:** boot.py per-op feeding (§3), installer network
   restructure to progress-fed reads (§4), `openmv_wdt` reworked to select the deep-sleep-safe
   watchdog per board (`"WWDG"` on N6) + window-aware `feed()`/`FEED_HZ` (§5), and **cherry-pick
   micropython#19350 (STM32 WWDG) + #19399 (ALIF WDT) into the fork via
   `project._ensure_ota_firmware_features`** — the exact mechanism that already carries #19348 (add a
   per-PR commit list + a sentinel symbol; it fetches `pull/NNNN/head`, cherry-picks, and commits the
   bump to keep the tree clean for the lock).

   **Gating note:** the make-or-break erase measurement (§6.4) uses the *current* firmware and needs
   none of these cherry-picks. Do it first — if a single sector erase can't fit or keep IRQs live
   under ~100 ms, the whole WWDG effort (cherry-picks + rework) is questionable and we regroup before
   investing in it.
4. **Part C.** Reference main.py + contract docs (§5).
5. **Part D1.** `ENABLED = True` firmware; positive `watchdog` scenario (start at board-max timeout,
   tighten toward target). **Recovery path ready** (re-flash an `ENABLED = False` golden).
6. **Part D2/D3.** Negative `watchdog_bite` + boot-under-WDT.
7. **Part E.** Coverage closure + audit-gate green.

**Findings that could change scope (surface early):**
- **§6.1** — if only IWDG is reachable, deep-sleep apps can't use our watchdog until `openmv_wdt` is
  reworked to drive WWDG. Potentially the biggest piece of work here.
- **§6.4** — if a single sector erase exceeds the window and can't keep IRQs live, a ~100 ms watchdog
  is **incompatible with our in-place erase**: we'd need a larger window (documented as a deep-sleep
  trade-off) or a different erase strategy. The make-or-break measurement.
- **§4** — the installer network restructure (short progress-fed reads replacing one blocking `recv`)
  is real work on a hot path; confirm before I start.
